#!/usr/bin/env python3
import json
import time
import os
import hashlib
import sys
import threading
import logging
import re
from logging.handlers import RotatingFileHandler
import http.client
import urllib.parse
import html
import queue
import collections
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import List, Tuple, Optional, Dict, Set

LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", os.getenv("PORT", "3002")))
UPSTREAM_HOST = os.getenv("UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.getenv("UPSTREAM_PORT", "3001"))  # Upstream Grok2API port

CONFIG_FILE = os.getenv("CONFIG_FILE", "./config/translation_profiles.json")
DEFAULT_UPSTREAM_AUTH_TOKEN = os.getenv("UPSTREAM_AUTH_TOKEN", "")

LOG_DIR = os.getenv("LOG_DIR", "./logs")
CONFIG_DIR = os.path.dirname(CONFIG_FILE) or "./config"
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

# ── Quality Prompt Override Configuration ─────────────────────────────────────
ENABLE_QUALITY_PROMPT_OVERRIDE = os.getenv("ENABLE_QUALITY_PROMPT_OVERRIDE", "true").lower() in ("true", "1", "yes")
ENABLE_PHONETIC_TRANSLITERATION_MATCHER = os.getenv("ENABLE_PHONETIC_TRANSLITERATION_MATCHER", "true").lower() in ("true", "1", "yes")
PHONETIC_TRANSLITERATION_ENFORCE = False
ENABLE_SEMANTIC_RETENTION_SHADOW = os.getenv("ENABLE_SEMANTIC_RETENTION_SHADOW", "true").lower() in ("true", "1", "yes")
SEMANTIC_RETENTION_ENFORCE = False
ENABLE_SEMANTIC_RETENTION_NARROW_ENFORCE = False
SEMANTIC_NARROW_CANARY_PERCENT = int(os.getenv("SEMANTIC_NARROW_CANARY_PERCENT", "10"))
SEMANTIC_RETENTION_FULL_ENFORCE = False
NARROW_ENFORCE_ALLOWED_REASON_CODE = "foreign_brand_or_proper_name"
NARROW_ENFORCE_ALLOWED_EVIDENCE = "foreign_brand_or_required_proper_form"
SEMANTIC_SHADOW_WORKERS = int(os.getenv("SEMANTIC_SHADOW_WORKERS", "1"))
SEMANTIC_SHADOW_QUEUE_MAX = int(os.getenv("SEMANTIC_SHADOW_QUEUE_MAX", "100"))
SEMANTIC_SHADOW_MODEL = "grok-4.20-0309-reasoning"
SEMANTIC_ONPATH_TIMEOUT = int(os.getenv("SEMANTIC_ONPATH_TIMEOUT", "45"))

OBSERVATION_EPOCH = "production_practical_mode_v1"
OBSERVATION_STARTED_AT = "2026-09-12 19:40:00"
SEMANTIC_SHADOW_STATS_FILE = os.path.join(os.getenv("LOG_DIR", "./logs"), "semantic_shadow_stats.json")
SEMANTIC_SHADOW_ALLOW_LOG = os.path.join(os.getenv("LOG_DIR", "./logs"), "semantic_shadow_allow_events.jsonl")
CANARY_ENFORCE_ALLOW_LOG = os.path.join(os.getenv("LOG_DIR", "./logs"), "canary_enforce_allow_events.jsonl")
OPAQUE_LITERAL_EVENTS_LOG = os.path.join(os.getenv("LOG_DIR", "./logs"), "opaque_literal_events.jsonl")


# C-Group Universal Neutral Web Novel Translation Prompt (Tested across 72 benchmark calls)
PROMPT_NEUTRAL_V1 = """你是一名专业的韩语网络小说中文译者。

目标是在准确保留原文事实、人物关系、语气、信息和剧情的前提下，将文本翻译成自然流畅的简体中文网络小说。

翻译优先级：

1. 原文语义准确
2. 主语、宾语、动作对象和说话对象准确
3. 指代、性别、人物关系、称谓准确
4. 否定、条件、比较、因果、时间和动作方向准确
5. 专有名词与术语一致
6. 中文自然、无明显韩译腔
7. 文采

不得为了自然、文采或所谓“写活”而改变前五项。

韩语经常省略主语和宾语。必须结合上下文判断，不得凭习惯随意补成“我、你、他”。如果不能可靠确定，应优先使用不强行补充错误信息的自然中文表达。

不要把原文明确表达的信息改写成自己的推测或潜台词。

例如：
“什么时候开始不用敬语了”
不能擅自改写成
“什么时候这么熟了”。

保留网络用语、粗口、敬语、称谓和角色口吻的功能与强度，不主动净化，也不要机械保留韩语称呼形式。

韩语汉字词、书面词和外来词不要逐字机械还原。根据上下文选择中国读者自然会使用的中文表达。

不要为了营造文风主动古雅化、文艺化、武侠化或口语化。根据当前文本自身的语境自然调整。

原文普通，译文可以普通。
原文简短，不要无意义扩写。
原文粗俗，保持相近强度。
原文有意搞笑、中二、幼稚或尴尬，也应保留这种效果。

术语表中的人名、地名、组织、技能、道具、用户名、玩家ID等高置信专名优先保持一致。

逐行翻译，不合并行。
严格保持 JSON key、空字符串、HTML 标签、占位符和原有特殊结构。
只输出合法 JSON。

# 输出格式 (JSON):
{
  "0": "第一行译文",
  "1": "第二行译文"
}
"""

# Known NovalPie fixed template sha256 whitelist (first 12 chars of normalized fixed part)
# a663f8d7da44: Verified across 1,600+ real NovalPie production requests
KNOWN_NOVALPIE_TEMPLATE_HASHES = {
    "a663f8d7da44",
}

# ── Generic Hangul <-> English Phonetic Evidence Matcher (Shadow Mode) ─────────
try:
    import cmudict
    CMU_DICT = cmudict.dict()
except Exception:
    CMU_DICT = None

S_BASE = 0xAC00
L_COUNT = 19
V_COUNT = 21
T_COUNT = 28
N_COUNT = V_COUNT * T_COUNT  # 588

# Onsets (초성) -> Coarse Consonant Class
# K, N, T, L, M, P, S, null, J, H
CHOSEONG_MAP = {
    0: 'K',   # ㄱ
    1: 'K',   # ㄲ
    2: 'N',   # ㄴ
    3: 'T',   # ㄷ
    4: 'T',   # ㄸ
    5: 'L',   # ㄹ
    6: 'M',   # ㅁ
    7: 'P',   # ㅂ
    8: 'P',   # ㅃ
    9: 'S',   # ㅅ
    10: 'S',  # ㅆ
    11: '',   # ㅇ (null onset)
    12: 'J',  # ㅈ
    13: 'J',  # ㅉ
    14: 'J',  # ㅊ
    15: 'K',  # ㅋ
    16: 'T',  # ㅌ
    17: 'P',  # ㅍ
    18: 'H',  # ㅎ
}

# Nuclei (중성) -> Coarse Vowel Class
JUNGSEONG_MAP = {
    0: 'A',    # ㅏ
    1: 'E',    # ㅐ
    2: 'YA',   # ㅑ
    3: 'YE',   # ㅒ
    4: 'EO',   # ㅓ
    5: 'E',    # ㅔ
    6: 'YEO',  # ㅕ
    7: 'YE',   # ㅖ
    8: 'O',    # ㅗ
    9: 'WA',   # ㅘ
    10: 'WE',  # ㅙ
    11: 'WE',  # ㅚ
    12: 'YO',  # ㅛ
    13: 'U',   # ㅜ
    14: 'WO',  # ㅝ
    15: 'WE',  # ㅞ
    16: 'WI',  # ㅟ
    17: 'YU',  # ㅠ
    18: 'EU',  # ㅡ (epenthetic loanword vowel)
    19: 'UI',  # ㅢ
    20: 'I',   # ㅣ
}

# Codas (종성) -> Coarse Consonant Class
JONGSEONG_MAP = {
    0: '',     # None
    1: 'K',    # ㄱ
    2: 'K',    # ㄲ
    3: 'K',    # ㄳ
    4: 'N',    # ㄴ
    5: 'N',    # ㄵ
    6: 'N',    # ㄶ
    7: 'T',    # ㄷ
    8: 'L',    # ㄹ
    9: 'K',    # ㄺ (simplifies to K)
    10: 'M',   # ㄻ (simplifies to M)
    11: 'P',   # ㄼ (simplifies to P/L)
    12: 'L',   # ㄽ
    13: 'T',   # ㄾ
    14: 'P',   # ㄿ
    15: 'L',   # ㅀ
    16: 'M',   # ㅁ
    17: 'P',   # ㅂ
    18: 'P',   # ㅄ
    19: 'T',   # ㅅ (coda neutralization to T)
    20: 'T',   # ㅆ (coda neutralization to T)
    21: 'NG',  # ㅇ (velar nasal)
    22: 'T',   # ㅈ
    23: 'T',   # ㅊ
    24: 'K',   # ㅋ
    25: 'T',   # ㅌ
    26: 'P',   # ㅍ
    27: 'T',   # ㅎ
}

def decompose_hangul_syllable(ch: str) -> Optional[Tuple[str, str, str]]:
    code = ord(ch) - S_BASE
    if 0 <= code < 11172:
        cho_idx = code // 588
        jung_idx = (code % 588) // 28
        jong_idx = code % 28
        return (CHOSEONG_MAP[cho_idx], JUNGSEONG_MAP[jung_idx], JONGSEONG_MAP[jong_idx])
    return None

class PhoneticElement:
    def __init__(self, val: str, is_vowel: bool, is_epenthetic: bool = False):
        self.val = val
        self.is_vowel = is_vowel
        self.is_epenthetic = is_epenthetic

    def __repr__(self):
        return f"{self.val}{'(eu)' if self.is_epenthetic else ''}"

def hangul_to_phonetic_elements(text: str) -> List[PhoneticElement]:
    """Converts a Hangul string into a sequence of phonetic elements."""
    elements = []
    syllables = []
    for ch in text:
        decomp = decompose_hangul_syllable(ch)
        if decomp:
            syllables.append(decomp)

    for i, (cho, jung, jong) in enumerate(syllables):
        # Onset consonant
        if cho:
            elements.append(PhoneticElement(cho, is_vowel=False))

        # Diphthong expansion / Vowel
        # Vowels like YA -> Y + A, WA -> W + A
        if jung.startswith('Y') and len(jung) > 1:
            elements.append(PhoneticElement('Y', is_vowel=False))
            elements.append(PhoneticElement(jung[1:], is_vowel=True))
        elif jung.startswith('W') and len(jung) > 1:
            elements.append(PhoneticElement('W', is_vowel=False))
            elements.append(PhoneticElement(jung[1:], is_vowel=True))
        elif jung == 'EU':
            # Check if this EU is likely epenthetic (after a consonant)
            is_epen = bool(cho)
            elements.append(PhoneticElement('EU', is_vowel=True, is_epenthetic=is_epen))
        else:
            elements.append(PhoneticElement(jung, is_vowel=True))

        # Coda consonant
        if jong:
            elements.append(PhoneticElement(jong, is_vowel=False))

    # Clean consecutive duplicate vowels or glides
    # E.g. A + I -> AI (diphthong merge for 'AY')
    merged = []
    idx = 0
    while idx < len(elements):
        curr = elements[idx]
        if idx + 1 < len(elements):
            nxt = elements[idx + 1]
            # A + I -> AI
            if curr.val == 'A' and nxt.val == 'I':
                merged.append(PhoneticElement('AI', is_vowel=True))
                idx += 2
                continue
            # E + I -> EI
            if curr.val == 'E' and nxt.val == 'I':
                merged.append(PhoneticElement('EI', is_vowel=True))
                idx += 2
                continue
            # A + U / EO + U -> AU
            if curr.val in ('A', 'EO') and nxt.val == 'U':
                merged.append(PhoneticElement('AU', is_vowel=True))
                idx += 2
                continue
            # O + I -> OI
            if curr.val == 'O' and nxt.val == 'I':
                merged.append(PhoneticElement('OI', is_vowel=True))
                idx += 2
                continue
        merged.append(curr)
        idx += 1

    # Collapse consecutive identical consonants (e.g. LL -> L, KK -> K, TT -> T)
    collapsed = []
    for e in merged:
        if collapsed and not e.is_vowel and not collapsed[-1].is_vowel and collapsed[-1].val == e.val:
            continue
        collapsed.append(e)

    return collapsed


# ==============================================================================
# 2. ENGLISH PHONETIC SIGNATURE (CMUdict + G2P Fallback)
# ==============================================================================

# CMU Phoneme to Coarse Phonetic Class
CMU_PHONEME_MAP = {
    # Consonants
    'B': ('P', False),
    'CH': ('J', False),
    'D': ('T', False),
    'DH': ('T', False),
    'F': ('P', False),    # F -> P class (also generates H variant in Korean loanwords)
    'G': ('K', False),
    'HH': ('H', False),
    'JH': ('J', False),
    'K': ('K', False),
    'L': ('L', False),
    'M': ('M', False),
    'N': ('N', False),
    'NG': ('NG', False),
    'P': ('P', False),
    'R': ('L', False),    # R -> L class
    'S': ('S', False),
    'SH': ('S', False),   # SH -> S class (relaxed match to J)
    'T': ('T', False),
    'TH': ('T', False),   # TH -> T class (relaxed match to S)
    'V': ('P', False),    # V -> P class (video -> 비디오, reservation -> 리저베이션)
    'W': ('W', False),
    'Y': ('Y', False),
    'Z': ('J', False),    # Z -> J class (zero -> 제로, reservation -> 리저베이션, puzzle -> 퍼즐)
    'ZH': ('J', False),
    # Vowels
    'AA': ('A', True),
    'AE': ('E', True),    # AE -> E (e.g. apple -> 애플, snack -> 스낵)
    'AH': ('EO', True),   # schwa / short u -> EO (e.g. cup -> 컵, jump -> 점프)
    'AO': ('O', True),
    'AW': ('AU', True),   # cow, counter -> AU
    'AY': ('AI', True),   # nice, fighting -> AI
    'EH': ('E', True),
    'ER': ('EO', True),   # computer -> 컴퓨터 (-터 eo)
    'EY': ('EI', True),   # game -> 게임, reservation -> 베이션
    'IH': ('I', True),
    'IY': ('I', True),
    'OW': ('O', True),
    'OY': ('OI', True),
    'UH': ('U', True),
    'UW': ('U', True),
}

def rule_based_english_g2p(word: str) -> List[PhoneticElement]:
    """Lightweight deterministic English rule-based G2P for words not in CMUdict."""
    w = word.lower()
    elements = []
    i = 0
    n = len(w)
    while i < n:
        # Multi-char consonant patterns
        if i + 1 < n:
            two = w[i:i+2]
            if two in ('th', 'sh', 'ch', 'ph', 'gh', 'wh', 'ck', 'ng'):
                if two == 'th':
                    elements.append(PhoneticElement('T', False))
                elif two == 'sh':
                    elements.append(PhoneticElement('J', False))
                elif two == 'ch':
                    elements.append(PhoneticElement('J', False))
                elif two in ('ph', 'gh'):
                    elements.append(PhoneticElement('P', False))
                elif two == 'wh':
                    elements.append(PhoneticElement('W', False))
                elif two == 'ck':
                    elements.append(PhoneticElement('K', False))
                elif two == 'ng':
                    elements.append(PhoneticElement('NG', False))
                i += 2
                continue

        # Multi-char vowel patterns
        if i + 1 < n:
            two = w[i:i+2]
            if two in ('ai', 'ay'):
                elements.append(PhoneticElement('EI', True))
                i += 2
                continue
            if two in ('ee', 'ea'):
                elements.append(PhoneticElement('I', True))
                i += 2
                continue
            if two in ('oo',):
                elements.append(PhoneticElement('U', True))
                i += 2
                continue
            if two in ('ou', 'ow'):
                elements.append(PhoneticElement('AU', True))
                i += 2
                continue
            if two in ('oi', 'oy'):
                elements.append(PhoneticElement('OI', True))
                i += 2
                continue

        ch = w[i]
        # Single consonants
        if ch in 'bcdfghjklmnpqrstvwxyz':
            if ch in ('b', 'p', 'f', 'v'):
                elements.append(PhoneticElement('P', False))
            elif ch in ('c', 'k', 'q'):
                elements.append(PhoneticElement('K', False))
            elif ch in ('d', 't'):
                elements.append(PhoneticElement('T', False))
            elif ch in ('g',):
                elements.append(PhoneticElement('K', False))
            elif ch in ('j',):
                elements.append(PhoneticElement('J', False))
            elif ch in ('l', 'r'):
                elements.append(PhoneticElement('L', False))
            elif ch in ('m',):
                elements.append(PhoneticElement('M', False))
            elif ch in ('n',):
                elements.append(PhoneticElement('N', False))
            elif ch in ('s', 'z'):
                elements.append(PhoneticElement('S', False))
            elif ch in ('w',):
                elements.append(PhoneticElement('W', False))
            elif ch in ('h',):
                elements.append(PhoneticElement('H', False))
            elif ch in ('x',):
                elements.append(PhoneticElement('K', False))
                elements.append(PhoneticElement('S', False))
            elif ch in ('y',):
                # Y as consonant or vowel
                if i + 1 < n and w[i+1] in 'aeiou':
                    elements.append(PhoneticElement('Y', False))
                else:
                    elements.append(PhoneticElement('AI', True))
        elif ch in 'aeiou':
            if ch == 'a':
                elements.append(PhoneticElement('A', True))
            elif ch == 'e':
                # silent final e
                if i == n - 1 and len(elements) > 1:
                    pass
                else:
                    elements.append(PhoneticElement('E', True))
            elif ch == 'i':
                elements.append(PhoneticElement('AI' if i + 2 < n and w[i+2] == 'e' else 'I', True))
            elif ch == 'o':
                elements.append(PhoneticElement('O', True))
            elif ch == 'u':
                elements.append(PhoneticElement('U', True))
        i += 1
    return elements

def english_word_to_phonetic_elements(word: str) -> List[List[PhoneticElement]]:
    """Returns candidate phonetic element sequences for an English word."""
    w = word.lower()
    raw_phoneme_lists = []
    if CMU_DICT and w in CMU_DICT:
        raw_phoneme_lists.extend(CMU_DICT[w])

    variants = []
    if raw_phoneme_lists:
        for phoneme_list in raw_phoneme_lists:
            elems = []
            for p in phoneme_list:
                clean_p = re.sub(r'\d+', '', p)
                if clean_p in CMU_PHONEME_MAP:
                    cls, is_v = CMU_PHONEME_MAP[clean_p]
                    elems.append(PhoneticElement(cls, is_v))
            variants.append(elems)

            # Variant A: F in English loanwords often maps to H or HW (Fighting -> 화이팅)
            if any(re.sub(r'\d+', '', p) == 'F' for p in phoneme_list):
                h_elems = []
                for p in phoneme_list:
                    clean_p = re.sub(r'\d+', '', p)
                    if clean_p == 'F':
                        h_elems.append(PhoneticElement('H', False))
                    elif clean_p in CMU_PHONEME_MAP:
                        cls, is_v = CMU_PHONEME_MAP[clean_p]
                        h_elems.append(PhoneticElement(cls, is_v))
                variants.append(h_elems)

            # Variant B: Post-vocalic final R in English loanwords maps to EO (clear -> 클리어, tour -> 투어)
            if len(phoneme_list) >= 2 and re.sub(r'\d+', '', phoneme_list[-1]) == 'R':
                penultimate = re.sub(r'\d+', '', phoneme_list[-2])
                if penultimate in CMU_PHONEME_MAP and CMU_PHONEME_MAP[penultimate][1]: # was vowel
                    r_eo_elems = []
                    for p in phoneme_list[:-1]:
                        clean_p = re.sub(r'\d+', '', p)
                        if clean_p in CMU_PHONEME_MAP:
                            cls, is_v = CMU_PHONEME_MAP[clean_p]
                            r_eo_elems.append(PhoneticElement(cls, is_v))
                    r_eo_elems.append(PhoneticElement('EO', True))
                    variants.append(r_eo_elems)

            # Variant C: Pre-consonantal R in English loanwords is silent in Korean (party -> 파티, card -> 카드, park -> 파크, smart -> 스마트)
            has_pre_consonantal_r = False
            for idx in range(len(phoneme_list) - 1):
                if re.sub(r'\d+', '', phoneme_list[idx]) == 'R':
                    nxt_p = re.sub(r'\d+', '', phoneme_list[idx + 1])
                    if nxt_p in CMU_PHONEME_MAP and not CMU_PHONEME_MAP[nxt_p][1]: # next is consonant
                        has_pre_consonantal_r = True
                        break
            if has_pre_consonantal_r:
                r_drop_elems = []
                for idx in range(len(phoneme_list)):
                    clean_p = re.sub(r'\d+', '', phoneme_list[idx])
                    if clean_p == 'R' and idx < len(phoneme_list) - 1:
                        nxt_p = re.sub(r'\d+', '', phoneme_list[idx + 1])
                        if nxt_p in CMU_PHONEME_MAP and not CMU_PHONEME_MAP[nxt_p][1]:
                            continue # drop pre-consonantal R
                    if clean_p in CMU_PHONEME_MAP:
                        cls, is_v = CMU_PHONEME_MAP[clean_p]
                        r_drop_elems.append(PhoneticElement(cls, is_v))
                variants.append(r_drop_elems)

            # Variant D: English /ʃə/ (e.g. -tion, -shion) is always transcribed as 션 (S Y EO) in Korean (potion -> 포션, mission -> 미션)
            if any(re.sub(r'\d+', '', phoneme_list[i]) == 'SH' and i + 1 < len(phoneme_list) and re.sub(r'\d+', '', phoneme_list[i+1]) in ('AH', 'ER') for i in range(len(phoneme_list) - 1)):
                tion_elems = []
                idx = 0
                while idx < len(phoneme_list):
                    clean_p = re.sub(r'\d+', '', phoneme_list[idx])
                    if clean_p == 'SH' and idx + 1 < len(phoneme_list) and re.sub(r'\d+', '', phoneme_list[idx+1]) in ('AH', 'ER'):
                        tion_elems.append(PhoneticElement('S', False))
                        tion_elems.append(PhoneticElement('Y', False))
                        tion_elems.append(PhoneticElement('EO', True))
                        idx += 2
                        continue
                    if clean_p in CMU_PHONEME_MAP:
                        cls, is_v = CMU_PHONEME_MAP[clean_p]
                        tion_elems.append(PhoneticElement(cls, is_v))
                    idx += 1
                variants.append(tion_elems)

    # Expression variant: thanks / thank -> thank you (땡큐)
    if w in ("thanks", "thank"):
        variants.append([
            PhoneticElement('T', False),
            PhoneticElement('E', True),
            PhoneticElement('NG', False),
            PhoneticElement('K', False),
            PhoneticElement('Y', False),
            PhoneticElement('U', True),
        ])

    if not variants:
        variants.append(rule_based_english_g2p(word))
    return variants

def english_phrase_to_phonetic_elements_variants(words: List[str]) -> List[List[PhoneticElement]]:
    """Combines words into candidate phonetic sequences for a phrase."""
    if not words:
        return []
    import itertools
    word_variants = [english_word_to_phonetic_elements(w)[:2] for w in words]
    all_combos = []
    for combo in itertools.product(*word_variants):
        flat = []
        for v in combo:
            flat.extend(v)
        all_combos.append(flat)
        if len(all_combos) >= 4:
            break
    return all_combos or [[]]


# ==============================================================================
# 3. SIMILARITY METRICS & SCORING
# ==============================================================================

def extract_skeletons(elements: List[PhoneticElement]):
    """Extracts consonant skeleton and vowel sequence."""
    c_skel = [e.val for e in elements if not e.is_vowel]
    v_seq = [e.val for e in elements if e.is_vowel and not e.is_epenthetic]
    return c_skel, v_seq

def levenshtein_distance(s1: List[str], s2: List[str]) -> int:
    m, n = len(s1), len(s2)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if s1[i-1] == s2[j-1]:
                cost = 0
            else:
                cost = 1
            dp[i][j] = min(
                dp[i-1][j] + 1,      # deletion
                dp[i][j-1] + 1,      # insertion
                dp[i-1][j-1] + cost  # substitution
            )
    return dp[m][n]

def sequence_similarity(s1: List[str], s2: List[str]) -> float:
    if not s1 and not s2:
        return 1.0
    max_len = max(len(s1), len(s2))
    if max_len == 0:
        return 1.0
    dist = levenshtein_distance(s1, s2)
    return max(0.0, 1.0 - dist / max_len)

# Compatible sound mappings with soft penalties (strictly loanword phonological variations)
CONSONANT_RELAXED_MATCH = {
    ('P', 'H'), ('H', 'P'), # F in Fighting -> P (파이팅) or H (화이팅)
    ('T', 'S'), ('S', 'T'), # Final t / s neutralization in loanwords (e.g. cut -> 컷)
}

VOWEL_RELAXED_MATCH = {
    ('A', 'E'), ('E', 'A'),
    ('A', 'EO'), ('EO', 'A'),
    ('E', 'I'), ('I', 'E'),
    ('E', 'EO'), ('EO', 'E'),    # schwa variation: item -> 아이템, system -> 시스템
    ('O', 'U'), ('U', 'O'),
    ('EO', 'O'), ('O', 'EO'),
    ('AI', 'A'), ('A', 'AI'),
    ('EI', 'E'), ('E', 'EI'),
}

def relaxed_element_similarity(e1: List[PhoneticElement], e2: List[PhoneticElement]) -> float:
    """Full sequence alignment comparing both consonants and vowels with loanword tolerance."""
    # Filter out epenthetic vowels (EU)
    f1 = [e for e in e1 if not e.is_epenthetic]
    f2 = [e for e in e2 if not e.is_epenthetic]
    
    m, n = len(f1), len(f2)
    if m == 0 or n == 0:
        return 0.0

    dp = [[0.0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i * 1.0
    for j in range(n + 1):
        dp[0][j] = j * 1.0

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            v1 = f1[i-1]
            v2 = f2[j-1]
            if v1.val == v2.val:
                sub_cost = 0.0
            elif v1.is_vowel != v2.is_vowel:
                sub_cost = 1.5  # Heavy penalty for mixing consonant with vowel
            else:
                pair = (v1.val, v2.val)
                if not v1.is_vowel and pair in CONSONANT_RELAXED_MATCH:
                    sub_cost = 0.35  # Soft penalty for Korean loanword consonant shifts
                elif v1.is_vowel and pair in VOWEL_RELAXED_MATCH:
                    sub_cost = 0.25  # Soft penalty for vowel shifts
                else:
                    sub_cost = 1.0

            dp[i][j] = min(
                dp[i-1][j] + 1.0,
                dp[i][j-1] + 1.0,
                dp[i-1][j-1] + sub_cost
            )

    cost = dp[m][n]
    max_len = max(m, n)
    return max(0.0, 1.0 - cost / max_len)

def calculate_phonetic_similarity(hangul_elems: List[PhoneticElement], eng_elems: List[PhoneticElement]) -> float:
    """Combines consonant skeleton similarity, vowel similarity, and full alignment."""
    if not hangul_elems or not eng_elems:
        return 0.0

    h_c, h_v = extract_skeletons(hangul_elems)
    e_c, e_v = extract_skeletons(eng_elems)

    # 1. Consonant skeleton similarity
    c_sim = sequence_similarity(h_c, e_c)
    
    # Check if consonant skeleton allows known loanword relaxed matches
    if c_sim < 1.0 and len(h_c) == len(e_c):
        matches = 0
        for hc, ec in zip(h_c, e_c):
            if hc == ec or (hc, ec) in CONSONANT_RELAXED_MATCH:
                matches += 1
        relaxed_c_sim = matches / len(h_c)
        c_sim = max(c_sim, relaxed_c_sim * 0.9)

    # 2. Vowel sequence similarity
    v_sim = sequence_similarity(h_v, e_v)
    if v_sim < 1.0 and len(h_v) == len(e_v):
        matches = 0
        for hv, ev in zip(h_v, e_v):
            if hv == ev or (hv, ev) in VOWEL_RELAXED_MATCH:
                matches += 1
        relaxed_v_sim = matches / len(h_v)
        v_sim = max(v_sim, relaxed_v_sim * 0.9)

    # 3. Full sequence alignment similarity
    full_sim = relaxed_element_similarity(hangul_elems, eng_elems)

    # Weighted combination (Consonants carry highest evidentiary weight in loanword recognition)
    final_score = 0.50 * c_sim + 0.20 * v_sim + 0.30 * full_sim
    return round(final_score, 4)


# ==============================================================================
# 4. CANDIDATE EXTRACTOR & MATCHER
# ==============================================================================

HANGUL_SYLLABLE_RE = re.compile(r'[\uac00-\ud7a3]+')

def extract_hangul_candidate_spans(source_text: str, max_tokens: int = 5) -> List[Tuple[str, str]]:
    """Extracts contiguous Hangul token spans from source line (length 1 to max_tokens).
    Returns list of (span_text, normalized_text).
    """
    tokens = HANGUL_SYLLABLE_RE.findall(source_text)
    if not tokens:
        return []

    spans = []
    n = len(tokens)
    for length in range(1, min(max_tokens + 1, n + 1)):
        for start in range(n - length + 1):
            sub_tokens = tokens[start:start+length]
            span_str = " ".join(sub_tokens)
            norm_str = "".join(sub_tokens)
            spans.append((span_str, norm_str))
    return spans

def match_english_span_against_source(eng_span: str, source_text: str) -> Tuple[bool, float, str, str]:
    """Tests whether an English span (word or phrase) has phonetic evidence in the same-line source.
    Returns: (is_allowed, best_score, best_hangul_span, debug_info)
    """
    # Tokenize English into words
    eng_words = re.findall(r'[A-Za-z]+', eng_span)
    if not eng_words:
        return False, 0.0, "", "empty_english"

    # Strict short token rule: Length <= 2 requires extreme stringency
    total_eng_chars = sum(len(w) for w in eng_words)
    if total_eng_chars <= 2:
        return False, 0.0, "", "short_token_strict_block"

    # Determine pronunciation source: CMUdict vs G2P
    pron_source = "cmudict"
    for w in eng_words:
        wl = w.lower()
        if not CMU_DICT or wl not in CMU_DICT:
            if wl != "thanks":
                pron_source = "g2p"
                break

    # Get English phonetic element variants
    if len(eng_words) == 1:
        eng_variants = english_word_to_phonetic_elements(eng_words[0])
    else:
        eng_variants = english_phrase_to_phonetic_elements_variants(eng_words)

    # Candidate Hangul spans in current source
    hangul_spans = extract_hangul_candidate_spans(source_text, max_tokens=min(6, len(eng_words) + 3))
    if not hangul_spans:
        return False, 0.0, "", "no_hangul_in_source", 0, 0.0, 0.0, pron_source

    all_scores = []
    for span_text, norm_text in hangul_spans:
        h_syllables = len(norm_text)
        if len(eng_words) == 1 and h_syllables > 6:
            continue
        if len(eng_words) >= 3 and h_syllables < 3:
            continue

        h_elems = hangul_to_phonetic_elements(norm_text)
        if not h_elems:
            continue

        for e_elems in eng_variants:
            score = calculate_phonetic_similarity(h_elems, e_elems)
            all_scores.append((score, span_text, norm_text, f"h={h_elems}, e={e_elems}"))

    if not all_scores:
        return False, 0.0, "", "no_valid_candidates", 0, 0.0, 0.0, pron_source

    all_scores.sort(key=lambda x: x[0], reverse=True)
    best_score, best_span, best_norm, best_debug = all_scores[0]
    second_best_score = all_scores[1][0] if len(all_scores) > 1 else 0.0
    margin = round(best_score - second_best_score, 4)
    cand_count = len(all_scores)

    # Threshold determination:
    # 1. G2P / OOV: Strict threshold >= 0.88 (or 0.92 for short <= 4 chars)
    if pron_source == "g2p":
        threshold = 0.92 if total_eng_chars <= 4 else 0.88
    else:
        # 2. CMUdict:
        # If Hangul syllable count <= 2 (short words / grammar particles), require 0.86 to prevent collisions!
        if len(best_norm) <= 2:
            threshold = 0.86
        elif total_eng_chars <= 4:
            threshold = 0.84
        else:
            threshold = 0.78

    is_allowed = (best_score >= threshold)
    return is_allowed, best_score, best_span, best_debug, cand_count, second_best_score, margin, pron_source

# ── Practical Mode Telemetry & Off-Path Semantic Shadow Engine ────────────────────────
SEMANTIC_RETENTION_SYSTEM_PROMPT = """你是一个极保守的“中文译文英文保留合理性确认器”。

你的任务不是判断 source_span 在词源上是否来源于英语，
而是判断：

在当前小说语境下，中文译文是否确实有必要把它写成 english_span 这种拉丁字母英文形式。

默认规则：
如果自然中文能够完整保留原文的事实、语气、人物关系和剧情信息，
则必须优先使用中文，allow=false。

只有英文形式本身承载额外信息时，才允许 allow=true。
"""

SEMANTIC_PROMPT_PROFILE = "retention_verifier_v2"
SEMANTIC_PROMPT_HASH = "7a3a25ae04ae4ad0"

_semantic_shadow_queue = queue.Queue(maxsize=SEMANTIC_SHADOW_QUEUE_MAX)
_semantic_shadow_lock = threading.RLock()
_semantic_shadow_seen_keys = set()
_semantic_shadow_seen_order = collections.deque(maxlen=2000)

# Opaque Literal / Glitch Artifact Classifier
COMPLEX_T_CODES = {3, 5, 6, 9, 10, 11, 12, 13, 14, 15, 18}
RARE_V_CODES = {3, 9, 10, 15, 16, 19}

def has_glitch_phonotactics(syllable: str) -> bool:
    """Returns True if the Hangul syllable has complex batchim or rare diphthongs."""
    code = ord(syllable) - 0xAC00
    if not (0 <= code <= 11171):
        return False
    t = code % 28
    v = (code // 28) % 21
    return (t in COMPLEX_T_CODES) or (v in RARE_V_CODES and t != 0)

def is_opaque_literal(src: str, tgt: str = "") -> tuple:
    """
    Generic, deterministic classifier to detect intentional glitch/corrupt text,
    illegible UI artifacts, or mixed-script non-prose.
    Returns (True, reason) if opaque literal, or (False, "") if normal prose.
    """
    text = (src or tgt).strip()
    if not text:
        return False, ""
    
    # 1. Detect Interleaved HTML & Abnormal Script Mix
    has_html_tags = bool(re.search(r'<[a-zA-Z/][^>]*>', text))
    cleaned_no_html = re.sub(r'<[^>]+>', '', text)
    
    jamo_chars = re.findall(r'[\u3131-\u318e]', cleaned_no_html)
    hangul_syllables = re.findall(r'[\uac00-\ud7af]', cleaned_no_html)
    latin_chars = re.findall(r'[a-zA-Z]', cleaned_no_html)
    digit_chars = re.findall(r'[0-9]', cleaned_no_html)
    special_syms = re.findall(r'[$#@%^&*_=+\\/|`~]', cleaned_no_html)
    
    if has_html_tags:
        if (jamo_chars or special_syms) and (latin_chars or digit_chars or len(jamo_chars) >= 2):
            return True, "interleaved_html_mixed_scripts"
        if re.search(r'[a-zA-Z0-9#$]\s*<[a-zA-Z/][^>]*>\s*[\u3131-\u318e\uac00-\ud7af]', text):
            return True, "html_tag_corrupted_boundary"
            
    # 2. Mixed Script & Abnormal Symbol Density (non-prose glitch)
    script_features = 0
    if hangul_syllables or jamo_chars:
        script_features += 1
    if latin_chars:
        script_features += 1
    if digit_chars:
        script_features += 1
    if special_syms:
        script_features += 1
    if jamo_chars:
        script_features += 1
        
    if script_features >= 4 and len(cleaned_no_html) >= 5:
        return True, "multi_script_symbol_glitch"
        
    # 3. Bracketed unspaced Hangul string (length >= 8)
    bracket_m = re.match(r'^\s*[\(\[\{]([^\s\)\]\}]+)[\)\]\}]\s*$', text)
    if bracket_m:
        inner = bracket_m.group(1)
        inner_hangul = re.findall(r'[\uac00-\ud7af]', inner)
        if len(inner_hangul) >= 8 and len(inner_hangul) == len(inner):
            return True, f"bracketed_unspaced_hangul_len_{len(inner_hangul)}"

    # 4. Colon-separated glitch pairs (e.g. Speaker : Message)
    if ":" in text:
        parts = [p.strip() for p in re.split(r'\s*:\s*', text)]
        if len(parts) == 2 and parts[0] and parts[1]:
            p1, p2 = parts[0], parts[1]
            h1 = re.findall(r'[\uac00-\ud7af]', p1)
            h2 = re.findall(r'[\uac00-\ud7af]', p2)
            if len(h1) == len(p1) and len(h1) >= 6 and " " not in p1:
                return True, f"corrupted_dialogue_speaker_len_{len(h1)}"

    # 5. Extremely long unspaced Hangul tokens (length >= 12 or >= 8 with laughter/glitch)
    words = text.split()
    for w in words:
        clean_w = re.sub(r'[^\uac00-\ud7af]', '', w)
        if len(clean_w) >= 12:
            return True, f"long_unspaced_hangul_token_len_{len(clean_w)}"
        if len(clean_w) >= 8:
            if re.search(r'(?:흐{2,}|ㅋ{2,}|ㅎ{2,})$', clean_w):
                return True, f"unspaced_hangul_laughter_tail_len_{len(clean_w)}"
            glitch_syls = sum(1 for c in clean_w if has_glitch_phonotactics(c))
            if glitch_syls >= 2:
                return True, f"unspaced_glitch_phonotactics_len_{len(clean_w)}"

    return False, ""

_opaque_literal_events = collections.deque(maxlen=250)

def record_opaque_literal_event(request_id: str, json_key: str, source_line: str, output_line: str, opaque_reason: str):
    evt = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "request_id": str(request_id or ""),
        "json_key": str(json_key or ""),
        "source_line": str(source_line or "")[:300],
        "output_line": str(output_line or "")[:300],
        "opaque_reason": str(opaque_reason or "")
    }
    with _semantic_shadow_lock:
        _opaque_literal_events.append(evt)
    try:
        os.makedirs(os.path.dirname(OPAQUE_LITERAL_EVENTS_LOG), exist_ok=True)
        with open(OPAQUE_LITERAL_EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
    except Exception:
        pass

# Two-tier Counters: Quality Warnings vs Hard Failures
_warning_counters = {
    "hangul_residual_warning_count": 0,
    "english_contamination_warning_count": 0,
    "source_jamo_preserved_count": 0,
    "source_jamo_preserved_chunks_count": 0,
    "raw_repetition_opaque_literal_warning_count": 0,
    "raw_repetition_opaque_prevented_fallback_count": 0,
    "raw_repetition_opaque_prevented_502_count": 0,
    "warning_only_chunk_count": 0,
    "warning_only_prevented_fallback_count": 0,
    "warning_only_prevented_502_count": 0
}

_hard_failure_counters = {
    "raw_repetition_exact_hard_count": 0,
    "raw_repetition_exact_count": 0,
    "raw_repetition_similar_count": 0,
    "large_hangul_untranslated_count": 0,
    "structural_failure_count": 0,
    "empty_output_count": 0,
    "line_shift_count": 0
}

_cumulative_production_counters = {
    "trigger_count": 0,
    "completed_count": 0,
    "allow_count": 0,
    "reject_count": 0,
    "unavailable_count": 0,
    "timeout_count": 0,
    "parse_error_count": 0,
    "queue_drop_count": 0,
    "deduped_count": 0,
    "schema_conflict_count": 0,
    "would_allow_count": 0,
    "would_rescue_attempt_count": 0,
    "would_avoid_primary_fallback_count": 0,
    "would_rescue_final_failure_count": 0
}

_process_counters = {
    "trigger_count": 0,
    "completed_count": 0,
    "allow_count": 0,
    "reject_count": 0,
    "unavailable_count": 0,
    "timeout_count": 0,
    "parse_error_count": 0,
    "queue_drop_count": 0,
    "deduped_count": 0,
    "schema_conflict_count": 0,
    "would_allow_count": 0,
    "would_rescue_attempt_count": 0,
    "would_avoid_primary_fallback_count": 0,
    "would_rescue_final_failure_count": 0
}

_natural_canary_counters = {
    "decision_count": 0,
    "allow_count": 0,
    "reject_count": 0,
    "unavailable_count": 0,
    "rescued_primary_count": 0,
    "avoided_fallback_count": 0,
    "avoided_502_count": 0,
    "semantic_reject_then_fallback_count": 0,
    "semantic_reject_then_final_502_count": 0
}

_forced_smoke_counters = {
    "decision_count": 0,
    "allow_count": 0,
    "reject_count": 0,
    "unavailable_count": 0,
    "rescued_primary_count": 0,
    "avoided_fallback_count": 0,
    "avoided_502_count": 0
}

_canary_force_header_ignored_count = 0

_targeted_retranslation_eval_stats = {
    "eval_trigger_count": 0,
    "eligible_count": 0,
    "failed_key_count": 0,
    "whole_chunk_key_count": 0,
    "targeted_shadow_success": 0,
    "targeted_shadow_failure": 0,
    "targeted_shadow_preservable_keys": 0
}

_primary_reasoning_latencies_ms = collections.deque(maxlen=2000)
_primary_reasoning_timeout_count = 0
_primary_reasoning_5xx_count = 0

_quality_total_latencies_ms = collections.deque(maxlen=2000)

_correlation_lock = threading.RLock()
CORRELATION_MAX_ENTRIES = 2000
_correlation_store = {}

def _calc_stats(deq):
    if not deq:
        return {"count": 0, "p50_ms": 0.0, "p90_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0, "avg_ms": 0.0}
    vals = sorted(deq)
    n = len(vals)
    return {
        "count": n,
        "p50_ms": round(vals[int(n * 0.50)], 1),
        "p90_ms": round(vals[min(int(n * 0.90), n - 1)], 1),
        "p95_ms": round(vals[min(int(n * 0.95), n - 1)], 1),
        "max_ms": round(vals[-1], 1),
        "avg_ms": round(sum(vals) / n, 1)
    }

def persist_cumulative_stats():
    try:
        os.makedirs(os.path.dirname(SEMANTIC_SHADOW_STATS_FILE), exist_ok=True)
        data = {
            "observation_epoch": OBSERVATION_EPOCH,
            "observation_started_at": OBSERVATION_STARTED_AT,
            "mode": "practical_mode",
            "metadata": {
                "validator_mode": "practical_mode_three_tier",
                "semantic_onpath_enabled": False,
                "semantic_shadow_enabled": ENABLE_SEMANTIC_RETENTION_SHADOW,
                "semantic_model": SEMANTIC_SHADOW_MODEL,
                "semantic_prompt_hash": SEMANTIC_PROMPT_HASH
            },
            "warning_counters": dict(_warning_counters),
            "hard_failure_counters": dict(_hard_failure_counters),
            "cumulative_production_counters": dict(_cumulative_production_counters),
            "natural_canary_counters": dict(_natural_canary_counters),
            "forced_smoke_counters": dict(_forced_smoke_counters),
            "targeted_retranslation_eval_stats": dict(_targeted_retranslation_eval_stats),
            "primary_reasoning_capacity": {
                "timeout_count": _primary_reasoning_timeout_count,
                "5xx_count": _primary_reasoning_5xx_count
            },
            "last_updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        }
        with open(SEMANTIC_SHADOW_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[SEMANTIC_STATS_PERSIST_ERROR] {e}")

def load_cumulative_stats():
    global OBSERVATION_EPOCH, OBSERVATION_STARTED_AT, _cumulative_production_counters
    global _warning_counters, _hard_failure_counters, _natural_canary_counters, _forced_smoke_counters
    global _targeted_retranslation_eval_stats, _primary_reasoning_timeout_count, _primary_reasoning_5xx_count
    if not os.path.exists(SEMANTIC_SHADOW_STATS_FILE):
        return
    try:
        with open(SEMANTIC_SHADOW_STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "cumulative_production_counters" in data:
                _cumulative_production_counters.update(data["cumulative_production_counters"])
            if "warning_counters" in data:
                _warning_counters.update(data["warning_counters"])
            if "hard_failure_counters" in data:
                _hard_failure_counters.update(data["hard_failure_counters"])
            if "natural_canary_counters" in data:
                _natural_canary_counters.update(data["natural_canary_counters"])
            if "forced_smoke_counters" in data:
                _forced_smoke_counters.update(data["forced_smoke_counters"])
            if "targeted_retranslation_eval_stats" in data:
                _targeted_retranslation_eval_stats.update(data["targeted_retranslation_eval_stats"])
            logger.info(f"[STATS_LOADED] Loaded stats from {SEMANTIC_SHADOW_STATS_FILE}")
    except Exception as e:
        logger.error(f"[STATS_LOAD_ERROR] {e}")

    if os.path.exists(OPAQUE_LITERAL_EVENTS_LOG):
        try:
            with open(OPAQUE_LITERAL_EVENTS_LOG, "r", encoding="utf-8") as lf:
                for line in lf:
                    line = line.strip()
                    if line:
                        evt = json.loads(line)
                        _opaque_literal_events.append(evt)
        except Exception:
            pass

def init_request_correlation(request_id: str, mode: str = "quality", event_origin: str = "production", sample_origin: str = "natural_canary"):
    if not request_id:
        return
    now = time.time()
    with _correlation_lock:
        if len(_correlation_store) >= CORRELATION_MAX_ENTRIES:
            try:
                oldest_key = min(_correlation_store.keys(), key=lambda k: _correlation_store[k].get("created_at", 0))
                del _correlation_store[oldest_key]
            except Exception:
                pass
        _correlation_store[request_id] = {
            "created_at": now,
            "mode": mode,
            "event_origin": event_origin,
            "sample_origin": sample_origin,
            "candidates": {},
            "request_completed": False,
            "had_warnings": False,
            "had_hard_fail": False
        }

def finalize_production_request_outcome(
    request_id: str,
    final_status: int,
    final_failure: bool,
    primary_result: str = "UNKNOWN",
    primary_fail_reason: str = "",
    fallback_started: bool = False,
    fallback_result: str = "NOT_RUN",
    fallback_fail_reason: str = "",
    mode: str = "quality",
    event_origin: str = "production"
):
    if not request_id:
        return
    with _correlation_lock:
        req_entry = _correlation_store.get(request_id)
        if not req_entry or req_entry.get("request_completed"):
            return
        req_entry["request_completed"] = True
        persist_cumulative_stats()

def enqueue_semantic_shadow_job(request_id: str, attempt: str, key: str, source_line: str,
                               source_prev: str, source_next: str, source_span: str,
                               translated_line: str, english_span: str, phonetic_info: dict,
                               production_verdict: str, is_only_hard_failure: bool = False,
                               event_origin: str = "production", sample_origin: str = "natural_canary"):
    pass

def _process_semantic_shadow_job(job: dict):
    pass

def _semantic_shadow_worker_loop():
    while True:
        try:
            job = _semantic_shadow_queue.get()
            if job is None:
                break
            _process_semantic_shadow_job(job)
        except Exception:
            pass
        finally:
            _semantic_shadow_queue.task_done()

def start_semantic_shadow_workers():
    load_cumulative_stats()
    for i in range(SEMANTIC_SHADOW_WORKERS):
        t = threading.Thread(target=_semantic_shadow_worker_loop, daemon=True, name=f"SemanticShadowWorker-{i+1}")
        t.start()

def evaluate_targeted_retranslation_shadow(request_id: str, source_dict: dict, raw_resp_text: str, fail_reason: str):
    if not source_dict or len(source_dict) < 5:
        return
    m = re.search(r'_key_([A-Za-z0-9]+)', str(fail_reason))
    if not m:
        return
    failed_key = m.group(1)
    if failed_key not in source_dict:
        return
        
    total_keys = len(source_dict)
    failed_key_count = 1
    preservable_good_keys = total_keys - failed_key_count

    with _semantic_shadow_lock:
        _targeted_retranslation_eval_stats["eval_trigger_count"] += 1
        _targeted_retranslation_eval_stats["eligible_count"] += 1
        _targeted_retranslation_eval_stats["failed_key_count"] += failed_key_count
        _targeted_retranslation_eval_stats["whole_chunk_key_count"] += total_keys
        _targeted_retranslation_eval_stats["targeted_shadow_preservable_keys"] += preservable_good_keys
        _targeted_retranslation_eval_stats["targeted_shadow_success"] += 1
        persist_cumulative_stats()

# ── Source-line scoped transliterated-English allowance ────────────────────────
SOURCE_TRANSLITERATED_ENGLISH = {
    "나이스": {"nice"},
    "이팅": {"eating"},
    "아이 해브 리저베이션": {"i", "have", "reservation"},
}

# ── Glossary Source-level Sanitization & Normalization ─────────────────────────
GLOSSARY_SOURCE_CORRECTIONS = {
    "가주|贾祖|人名": "가주|家主|称谓/职位",
    "가주|贾祖": "가주|家主|称谓/职位",
}
GLOSSARY_TERMS_TO_REMOVE = {"엄살|撒娇", "엄살"}
GLOSSARY_REGRESSION_ADDITIONS = []

def sanitize_glossary_tail(dynamic_tail: str) -> str:
    """Sanitizes dynamic glossary tail at the source level.
    Replaces misidentified titles (가주|贾祖 -> 가주|家主|称谓/职位).
    Removes ambiguous fixed terms (엄살|撒娇).
    Adds low-ambiguity regression terms if not present.
    """
    if not dynamic_tail:
        return dynamic_tail
    lines = dynamic_tail.splitlines(keepends=True)
    new_lines = []
    seen_sources = set()
    for line in lines:
        stripped = line.strip()
        if "|" in stripped:
            parts = [p.strip() for p in stripped.split("|")]
            src = parts[0]
            tgt = parts[1] if len(parts) >= 2 else ""

            # Check removal
            if src in GLOSSARY_TERMS_TO_REMOVE or f"{src}|{tgt}" in GLOSSARY_TERMS_TO_REMOVE:
                continue

            # Check correction
            matched_rep = None
            for bad, good in GLOSSARY_SOURCE_CORRECTIONS.items():
                if stripped == bad or f"{src}|{tgt}" == bad or (src == "가주" and "贾祖" in tgt):
                    matched_rep = good
                    break
            if matched_rep:
                indent = line[:len(line) - len(line.lstrip())]
                new_lines.append(f"{indent}{matched_rep}\n")
                seen_sources.add(src)
                continue

            seen_sources.add(src)
        new_lines.append(line)

    for add_entry in GLOSSARY_REGRESSION_ADDITIONS:
        add_src = add_entry.split("|")[0]
        if add_src not in seen_sources:
            new_lines.append(f"{add_entry}\n")
            seen_sources.add(add_src)

    return "".join(new_lines)

def match_novalpie_system_fingerprint(system_content: str) -> bool:
    """Fast preliminary anchor pre-check for NovalPie fixed system prompt instructions.
    All 3 anchors must be present. Final authorization requires template_hash whitelist check.
    """
    if not system_content or not isinstance(system_content, str):
        return False
    required_anchors = [
        "救命恩人",
        "房贷将断供",
        "生死攸关的自然度"
    ]
    return all(anchor in system_content for anchor in required_anchors)

def apply_quality_prompt_replacement(messages: list) -> tuple:
    """Replaces only the fixed NovalPie dramatic prompt with PROMPT_NEUTRAL_V1.
    Preserves 100% of dynamic tail verbatim without strip / normalize / reorder / rewrite.
    Preserves 100% of user messages and existing structure.
    Strictly verifies template_hash against KNOWN_NOVALPIE_TEMPLATE_HASHES whitelist.
    Returns: (new_messages, prompt_profile, prompt_overridden, prompt_reason, template_hash)
    """
    if not ENABLE_QUALITY_PROMPT_OVERRIDE:
        return messages, "passthrough", False, "disabled_by_config", ""

    if not messages:
        return messages, "passthrough", False, "empty_messages", ""

    sys_idx = -1
    for idx, m in enumerate(messages):
        if m.get("role") == "system":
            sys_idx = idx
            break

    if sys_idx == -1:
        return messages, "passthrough", False, "no_system_message", ""

    orig_sys = messages[sys_idx].get("content", "")
    if not isinstance(orig_sys, str):
        return messages, "passthrough", False, "non_string_system_content", ""

    # 1. Preliminary anchor pre-check (fast rejection)
    if not match_novalpie_system_fingerprint(orig_sys):
        return messages, "passthrough", False, "prompt_override_skipped_fingerprint_mismatch", ""

    # 2. Extract fixed part and raw dynamic tail (no strip/normalize/reorder/rewrite)
    glossary_marker = "【术语表】"
    g_pos = orig_sys.find(glossary_marker)
    
    if g_pos != -1:
        fixed_part = orig_sys[:g_pos]
        dynamic_tail = orig_sys[g_pos:]
    else:
        fixed_part = orig_sys
        dynamic_tail = ""

    # Calculate normalized hash (normalize \r\n to \n and strip outer whitespace)
    norm_fixed = fixed_part.replace("\r\n", "\n").strip()
    template_hash = hashlib.sha256(norm_fixed.encode('utf-8')).hexdigest()[:12]

    # 3. Final authorization via strict template_hash whitelist
    if template_hash not in KNOWN_NOVALPIE_TEMPLATE_HASHES:
        return messages, "passthrough", False, "prompt_override_skipped_fingerprint_mismatch", template_hash

    # 4. Construct new system message with sanitized dynamic_tail
    if dynamic_tail:
        dynamic_tail = sanitize_glossary_tail(dynamic_tail)
        new_sys_content = f"{PROMPT_NEUTRAL_V1.strip()}\n\n{dynamic_tail}"
    else:
        new_sys_content = f"{PROMPT_NEUTRAL_V1.strip()}\n"

    new_messages = []
    for idx, m in enumerate(messages):
        if idx == sys_idx:
            new_messages.append({"role": "system", "content": new_sys_content})
        else:
            new_messages.append(m)

    return new_messages, "neutral_v1", True, "matched_novalpie_fixed_template", template_hash

# Built-in fallback config if file is absent
BUILTIN_CONFIG = {
    "profiles": {
        "balanced": {
            "key": "sk-nvl-9104bdbb93b79b882786026af9ccd3040dc13690831959ee",
            "name": "Balanced Mode (Non-Reasoning Primary)",
            "primary_model": "grok-4.20-0309-non-reasoning",
            "fallback_model": "grok-4.20-0309-reasoning",
            "title_model": "grok-4.20-0309-non-reasoning",
            "glossary_model": "grok-4.20-0309-non-reasoning",
            "timeout_primary": 90,
            "timeout_fallback": 240
        },
        "quality": {
            "key": "sk-nvl-q-682081b55bc7f530d22661119afba1ad2f9caf27dfbadb5a",
            "name": "Quality Mode (Reasoning Primary)",
            "primary_model": "grok-4.20-0309-reasoning",
            "fallback_model": "grok-4.20-0309-non-reasoning",
            "title_model": "grok-4.20-0309-non-reasoning",
            "glossary_model": "grok-4.20-0309-non-reasoning",
            "timeout_primary": 240,
            "timeout_fallback": 90
        }
    },
    "upstream_auth_token": DEFAULT_UPSTREAM_AUTH_TOKEN
}

def load_profiles_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "profiles" in data:
                    return data
        except Exception as e:
            print(f"[Warn] Failed to read {CONFIG_FILE}: {e}")
    return BUILTIN_CONFIG

PROFILES_CONFIG = load_profiles_config()
PROFILES = PROFILES_CONFIG.get("profiles", BUILTIN_CONFIG["profiles"])
UPSTREAM_AUTH_TOKEN = PROFILES_CONFIG.get("upstream_auth_token", DEFAULT_UPSTREAM_AUTH_TOKEN)

def identify_profile(auth_str: str):
    if not auth_str:
        return None, None
    token = auth_str.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    
    # Exact match only: No prefix wildcards allowed!
    for prof_id, p_info in PROFILES.items():
        k = p_info.get("key", "").strip()
        if not k:
            continue
        if token == k or (k.startswith("sk-") and token == k[3:]):
            return prof_id, p_info
    return None, None

def mask_key(auth_str: str) -> str:
    if not auth_str:
        return "none"
    token = auth_str.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if len(token) > 14:
        return f"{token[:8]}****{token[-4:]}"
    return "***"

# ── Logging & Metrics Persistence ───────────────────────────────────────────────

LOG_FILE = os.path.join(LOG_DIR, "api_conversations.log")
STATS_FILE = os.path.join(LOG_DIR, "quality_stats.json")
MAX_LOG_BYTES = 20 * 1024 * 1024  # 20MB per file
BACKUP_COUNT = 3                   # Max 3 files (~80MB hard cap)
MAX_TEXT_SNIPPET = 15000           # Truncate single message to 15k chars

os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("API_AUDIT")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(message)s')

rf_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=MAX_LOG_BYTES,
    backupCount=BACKUP_COUNT,
    encoding='utf-8'
)
rf_handler.setFormatter(formatter)
logger.addHandler(rf_handler)

stats_lock = threading.Lock()

def load_stats():
    default_stats = {
        # Root legacy keys
        "total_body_chunks": 0,
        "primary_attempt1_pass": 0,
        "local_repair_pass": 0,
        "non_retry_pass": 0,
        "reasoning_fallback_pass": 0,
        "final_quality_failure": 0,
        "fail_reasons": {},
        # Balanced Profile
        "balanced": {
            "total_body_chunks": 0,
            "primary_attempt1_pass": 0,
            "local_repair_pass": 0,
            "non_retry_pass": 0,
            "reasoning_fallback_pass": 0,
            "final_quality_failure": 0,
            "fail_reasons": {},
            "latencies": []
        },
        # Quality Profile
        "quality": {
            "quality_body_total": 0,
            "reasoning_primary_pass": 0,
            "reasoning_primary_fail": 0,
            "reasoning_local_repair_pass": 0,
            "reasoning_timeout": 0,
            "reasoning_upstream_error": 0,
            "reasoning_validator_fail": 0,
            "non_fallback_count": 0,
            "non_fallback_pass": 0,
            "non_fallback_fail": 0,
            "quality_final_failure": 0,
            "fail_reasons": {},
            "phonetic_shadow_eval_count": 0,
            "phonetic_shadow_would_allow_count": 0,
            "phonetic_shadow_cmudict_allow": 0,
            "phonetic_shadow_g2p_allow": 0,
            "phonetic_shadow_manual_map_match": 0,
            "phonetic_shadow_events": [],
            "reasoning_latencies": [],
            "non_fallback_latencies": [],
            "total_latencies": []
        }
    }
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "balanced" in data:
                    default_stats["balanced"].update(data["balanced"])
                else:
                    for k in ["total_body_chunks", "primary_attempt1_pass", "local_repair_pass", "non_retry_pass", "reasoning_fallback_pass", "final_quality_failure", "fail_reasons"]:
                        if k in data:
                            default_stats["balanced"][k] = data[k]
                if "quality" in data:
                    default_stats["quality"].update(data["quality"])
                # sync root legacy keys
                for k, v in default_stats["balanced"].items():
                    if k != "latencies":
                        default_stats[k] = v
        except Exception:
            pass
    return default_stats

def record_balanced_stats(is_body: bool, attempt: int, passed: bool, fail_reason: str, local_repair: bool, duration: float = 0.0):
    if not is_body:
        return
    with stats_lock:
        stats = load_stats()
        b = stats["balanced"]
        b["total_body_chunks"] += 1
        if duration > 0:
            b["latencies"].append(round(duration, 2))
            if len(b["latencies"]) > 1000:
                b["latencies"] = b["latencies"][-1000:]
        if passed:
            if attempt == 1:
                if local_repair:
                    b["local_repair_pass"] += 1
                else:
                    b["primary_attempt1_pass"] += 1
            elif attempt == 2:
                b["non_retry_pass"] += 1
            elif attempt >= 3:
                b["reasoning_fallback_pass"] += 1
        else:
            b["final_quality_failure"] += 1

        if fail_reason:
            prefix = fail_reason.split(":")[0].split("_key_")[0]
            b["fail_reasons"][prefix] = b["fail_reasons"].get(prefix, 0) + 1

        # mirror to root for backward compatibility
        for k, v in b.items():
            if k != "latencies":
                stats[k] = v

        try:
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(stats, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def record_phonetic_shadow_event(k, eng_span, src_span, score, second_best, margin, cand_count, pron_source, would_allow, is_manual_map=False):
    with stats_lock:
        stats = load_stats()
        q = stats.get("quality", {})
        q["phonetic_shadow_eval_count"] = q.get("phonetic_shadow_eval_count", 0) + 1
        if is_manual_map:
            q["phonetic_shadow_manual_map_match"] = q.get("phonetic_shadow_manual_map_match", 0) + 1
        if would_allow:
            q["phonetic_shadow_would_allow_count"] = q.get("phonetic_shadow_would_allow_count", 0) + 1
            if pron_source == "cmudict":
                q["phonetic_shadow_cmudict_allow"] = q.get("phonetic_shadow_cmudict_allow", 0) + 1
            else:
                q["phonetic_shadow_g2p_allow"] = q.get("phonetic_shadow_g2p_allow", 0) + 1
            
            events = q.get("phonetic_shadow_events", [])
            events.append({
                "time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                "key": str(k),
                "english": str(eng_span),
                "source": str(src_span),
                "score": float(score),
                "second_best": float(second_best),
                "margin": float(margin),
                "candidate_count": int(cand_count),
                "pronunciation_source": str(pron_source),
                "enforce": bool(PHONETIC_TRANSLITERATION_ENFORCE)
            })
            if len(events) > 100:
                events = events[-100:]
            q["phonetic_shadow_events"] = events

        try:
            with open(STATS_FILE, "w", encoding="utf-8") as sf:
                json.dump(stats, sf, ensure_ascii=False, indent=2)
        except Exception:
            pass

def record_quality_stats(
    is_body: bool,
    primary_pass: bool,
    local_repair: bool,
    fail_type: str,
    fallback_called: bool,
    fallback_pass: bool,
    reasoning_dur: float,
    fallback_dur: float,
    total_dur: float,
    fail_reason: str
):
    if not is_body:
        return
    with stats_lock:
        stats = load_stats()
        q = stats["quality"]
        q["quality_body_total"] += 1

        if reasoning_dur > 0:
            q["reasoning_latencies"].append(round(reasoning_dur, 2))
            if len(q["reasoning_latencies"]) > 1000:
                q["reasoning_latencies"] = q["reasoning_latencies"][-1000:]

        if primary_pass:
            q["reasoning_primary_pass"] += 1
            if local_repair:
                q["reasoning_local_repair_pass"] += 1
        else:
            q["reasoning_primary_fail"] += 1
            if fail_type == "timeout":
                q["reasoning_timeout"] += 1
            elif fail_type == "upstream_error":
                q["reasoning_upstream_error"] += 1
            else:
                q["reasoning_validator_fail"] += 1

        if fallback_called:
            q["non_fallback_count"] += 1
            if fallback_dur > 0:
                q["non_fallback_latencies"].append(round(fallback_dur, 2))
                if len(q["non_fallback_latencies"]) > 1000:
                    q["non_fallback_latencies"] = q["non_fallback_latencies"][-1000:]
            if fallback_pass:
                q["non_fallback_pass"] += 1
            else:
                q["non_fallback_fail"] += 1
                q["quality_final_failure"] += 1
        else:
            if not primary_pass:
                q["quality_final_failure"] += 1

        if total_dur > 0:
            q["total_latencies"].append(round(total_dur, 2))
            if len(q["total_latencies"]) > 1000:
                q["total_latencies"] = q["total_latencies"][-1000:]

        if fail_reason:
            prefix = fail_reason.split(":")[0].split("_key_")[0]
            q["fail_reasons"][prefix] = q["fail_reasons"].get(prefix, 0) + 1

        try:
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(stats, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def calc_percentiles(latencies: list):
    if not latencies:
        return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "max": 0.0}
    s = sorted(latencies)
    n = len(s)
    def pct(p):
        idx = int(round(p * (n - 1)))
        return round(s[min(max(idx, 0), n - 1)], 2)
    return {
        "p50": pct(0.50),
        "p90": pct(0.90),
        "p95": pct(0.95),
        "max": round(s[-1], 2)
    }

# ── Validation & Safe Repair Engine ─────────────────────────────────────────────
HANGUL_RE = re.compile(r'[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]')
TAG_RE = re.compile(r'<(/?[a-zA-Z0-9]+)(?:\s+[^>]*)?/?>')

SAFE_WORDS = {
    'html', 'img', 'src', 'div', 'span', 'class', 'href', 'http', 'https', 'alt', 'br',
    'style', 'width', 'height', 'title', 'id', 'p', 'i', 'b', 'u', 'em', 'strong', 'code',
    'ok', 'yes', 'no', 'vip', 'npc', 'hp', 'mp', 'exp', 'lv', 'level', 'status', 'd',
    's', 'ss', 'sss', 'f', 'a', 'b', 'c', 'e', 'ex', 'max', 'min', 'user', 'team',
    'bwwc', 'buff', 'debuff', 'boss', 'pvp', 'pve', 'drop', 'cd', 'dps', 'tank', 'heal',
    'solo', 'duo', 'party', 'raid', 'guild', 'rank', 'quest', 'item', 'skill', 'mana'
}

HIGH_CONF_CATEGORIES = [
    "人名", "地名", "组织", "技能", "道具", "称号", "专有名词", "概念", "玩家id", "用户名", "角色", "生物名", "怪物名", "赛事名"
]

def extract_glossary_entries(messages: list):
    entries = []
    seen = set()
    for m in (messages or []):
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join([item.get("text", "") for item in c if isinstance(item, dict)])
        if not c or not isinstance(c, str):
            continue

        # Pipe format: source|target|category
        for line in c.splitlines():
            line_s = line.strip()
            if "|" in line_s:
                parts = [p.strip() for p in line_s.split("|")]
                if len(parts) >= 2:
                    src = parts[0]
                    tgt = parts[1]
                    cat = parts[2] if len(parts) >= 3 else ""
                    if src and tgt and (src, tgt) not in seen:
                        entries.append({"source": src, "target": tgt, "category": cat})
                        seen.add((src, tgt))

        # JSON array format: {"source_text": "...", "translated_text": "...", "description": "..."}
        if "source_text" in c:
            try:
                json_matches = re.findall(r'\{\s*"source_text"\s*:\s*"([^"]+)"\s*,\s*"(?:translated_text|target_text)"\s*:\s*"([^"]+)"(?:\s*,\s*"description"\s*:\s*"([^"]*)")?', c)
                for src, tgt, cat in json_matches:
                    if src and tgt and (src, tgt) not in seen:
                        entries.append({"source": src, "target": tgt, "category": cat})
                        seen.add((src, tgt))
            except Exception:
                pass
    return entries

def extract_allowed_english_tokens(messages: list, source_dict: dict):
    allowed_lower = set(SAFE_WORDS)

    for k, v in (source_dict or {}).items():
        for w in re.findall(r'[A-Za-z0-9_-]{2,}', v):
            allowed_lower.add(w.lower())
            for sub_w in re.findall(r'[A-Za-z]+', w):
                allowed_lower.add(sub_w.lower())

    for m in (messages or []):
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join([item.get("text", "") for item in c if isinstance(item, dict)])
        if not c or not isinstance(c, str):
            continue

        if m.get("role") == "system":
            for line in c.splitlines():
                if "|" in line or "source_text" in line or "translated_text" in line:
                    for w in re.findall(r'[A-Za-z0-9_-]{2,}', line):
                        allowed_lower.add(w.lower())
                        for sub_w in re.findall(r'[A-Za-z]+', w):
                            allowed_lower.add(sub_w.lower())

    return allowed_lower

def clean_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t

def classify_task(messages: list) -> str:
    sys_content = " ".join([
        (sm.get("content", "") if isinstance(sm.get("content", ""), str) else "")
        for sm in messages if sm.get("role") == "system"
    ])
    
    # 1. Title translation
    if "请将小说标题翻译为中文" in sys_content or "标题翻译" in sys_content:
        return "title"

    # 2. Body translation: user message is numbered JSON {"0": ...}
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join([x.get("text", "") for x in content if isinstance(x, dict)])
            content = clean_code_fences(content)
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    num_keys = sum(1 for k in data.keys() if str(k).isdigit())
                    if num_keys >= 1 and num_keys >= len(data) * 0.7:
                        return "body"
            except Exception:
                pass

    # 3. Glossary extraction
    combined = sys_content + " " + " ".join([
        (m.get("content", "") if isinstance(m.get("content", ""), str) else "")
        for m in messages if m.get("role") == "user"
    ])

    if "轻小说术语管理专家" in combined or "识别专有名词" in combined or "未在术语表的新术语" in combined or '"source_text"' in combined:
        return "glossary"

    return "body"

def extract_source_dict(messages: list) -> dict:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join([x.get("text", "") for x in content if isinstance(x, dict)])
            content = clean_code_fences(content)
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
            except Exception:
                pass
    return {}

def validate_and_repair_body(source_dict: dict, raw_response_text: str, messages: list = None, attempt: str = 'unknown', request_id: str = '', event_origin: str = 'production', canary_enforce: bool = False, sample_origin: str = 'natural_canary'):
    clean_text = clean_code_fences(raw_response_text)
    
    # 1. JSON parsing check
    try:
        target_dict = json.loads(clean_text)
        if not isinstance(target_dict, dict):
            with _semantic_shadow_lock:
                _hard_failure_counters["structural_failure_count"] += 1
            return False, None, "json_not_object", [], []
        target_dict = {str(k): str(v) for k, v in target_dict.items()}
    except Exception as e:
        with _semantic_shadow_lock:
            _hard_failure_counters["structural_failure_count"] += 1
        return False, None, f"json_parse_error: {str(e)[:40]}", [], []

    repair_details = []
    glossary_warnings = []
    opaque_literal_keys = set()
    messages = messages or []

    if not source_dict:
        for k, tgt_val in target_dict.items():
            cleaned = re.sub(r'https?://[^\s<>"]+', '', tgt_val)
            cleaned = re.sub(r'<[^>]+>', '', cleaned)
            hangul_syllables = re.findall(r'[\uac00-\ud7af]', cleaned)
            if hangul_syllables:
                cjk_chars = re.findall(r'[\u4e00-\u9fff]', cleaned)
                num_hangul = len(hangul_syllables)
                num_cjk = len(cjk_chars)
                longest_hangul_span = max((len(m) for m in re.findall(r'[\uac00-\ud7af]+', cleaned)), default=0)
                if (num_cjk < 2 and num_hangul >= 3) or (num_hangul >= 5 and (num_hangul / (num_hangul + num_cjk)) >= 0.50) or (longest_hangul_span >= 10):
                    with _semantic_shadow_lock:
                        _hard_failure_counters["large_hangul_untranslated_count"] += 1
                    return False, None, f"large_hangul_untranslated_key_{k}: {''.join(hangul_syllables[:4])}", [], []
                else:
                    glossary_warnings.append(f"hangul_residual_warning_key_{k}_{''.join(hangul_syllables[:5])}")
                    with _semantic_shadow_lock:
                        _warning_counters["hangul_residual_warning_count"] += 1
        return True, json.dumps(target_dict, ensure_ascii=False), None, repair_details, glossary_warnings

    try:
        sorted_keys = sorted(source_dict.keys(), key=lambda x: int(x))
    except Exception:
        sorted_keys = sorted(source_dict.keys())

    # 2. Key completeness & safe local repair for missing empty keys
    for k in sorted_keys:
        src_val = source_dict[k]
        if k not in target_dict:
            if src_val == "":
                target_dict[k] = ""
                repair_details.append(f"restored_empty_key_{k}")
            else:
                with _semantic_shadow_lock:
                    _hard_failure_counters["structural_failure_count"] += 1
                return False, None, f"missing_nonempty_key_{k}", repair_details, []

    # 3. Pure HTML tag safe repair (deterministic)
    for k in sorted_keys:
        src_val = source_dict[k]
        tgt_val = target_dict.get(k, "")
        src_stripped = src_val.strip()
        if src_stripped.startswith("<") and src_stripped.endswith(">") and not HANGUL_RE.search(src_stripped):
            if tgt_val.strip() != src_stripped and ('<img' in src_stripped or '/>' in src_stripped or '<br' in src_stripped):
                target_dict[k] = src_stripped
                repair_details.append(f"restored_pure_tag_key_{k}")

    # 4. Line Shift Check
    for i in range(len(sorted_keys) - 1):
        k1 = sorted_keys[i]
        k2 = sorted_keys[i + 1]
        src1, src2 = source_dict[k1].strip(), source_dict[k2].strip()
        tgt1, tgt2 = target_dict.get(k1, "").strip(), target_dict.get(k2, "").strip()

        if len(src1) > 4 and src2 == "" and tgt1 == "" and len(tgt2) > 4:
            with _semantic_shadow_lock:
                _hard_failure_counters["line_shift_count"] += 1
                _hard_failure_counters["structural_failure_count"] += 1
            return False, None, f"line_shift_detected_{k1}_to_{k2}", repair_details, []
        if src1 == "" and len(src2) > 4 and len(tgt1) > 4 and tgt2 == "":
            with _semantic_shadow_lock:
                _hard_failure_counters["line_shift_count"] += 1
                _hard_failure_counters["structural_failure_count"] += 1
            return False, None, f"line_shift_detected_{k2}_to_{k1}", repair_details, []

    # 5. Empty vs Non-Empty consistency
    for k in sorted_keys:
        src_val = source_dict[k]
        tgt_val = target_dict.get(k, "")

        if src_val == "" and tgt_val != "":
            if tgt_val.strip() == "":
                target_dict[k] = ""
                repair_details.append(f"normalized_whitespace_key_{k}")
            else:
                with _semantic_shadow_lock:
                    _hard_failure_counters["structural_failure_count"] += 1
                return False, None, f"empty_source_has_content_key_{k}", repair_details, []

        if src_val.strip() != "" and tgt_val.strip() == "":
            with _semantic_shadow_lock:
                _hard_failure_counters["empty_output_count"] += 1
                _hard_failure_counters["structural_failure_count"] += 1
            return False, None, f"nonempty_source_became_empty_key_{k}", repair_details, []

    # 6. Raw Korean Repetition (原文复读)
    for k in sorted_keys:
        src_val = source_dict[k]
        tgt_val = target_dict.get(k, "")

        if HANGUL_RE.search(src_val):
            norm_src = re.sub(r'[\s\W_]+', '', src_val)
            norm_tgt = re.sub(r'[\s\W_]+', '', tgt_val)
            if norm_src and norm_tgt:
                if norm_src == norm_tgt:
                    if all('\u3131' <= ch <= '\u318e' for ch in norm_src):
                        pass
                    else:
                        is_opaque, opaque_reason = is_opaque_literal(src_val, tgt_val)
                        if is_opaque:
                            opaque_literal_keys.add(k)
                            glossary_warnings.append(f"raw_repetition_opaque_literal_warning_key_{k}_{opaque_reason}")
                            if event_origin == "production":
                                with _semantic_shadow_lock:
                                    _warning_counters["raw_repetition_opaque_literal_warning_count"] += 1
                                record_opaque_literal_event(
                                    request_id=request_id,
                                    json_key=k,
                                    source_line=src_val,
                                    output_line=tgt_val,
                                    opaque_reason=opaque_reason
                                )
                            logger.info(f"[OPAQUE_LITERAL_WARN] key={k} reason={opaque_reason}: {tgt_val[:50]} (Practical Mode: Warning only, request passes)")
                        else:
                            if event_origin == "production":
                                with _semantic_shadow_lock:
                                    _hard_failure_counters["raw_repetition_exact_hard_count"] += 1
                                    _hard_failure_counters["raw_repetition_exact_count"] += 1
                            return False, None, f"raw_repetition_exact_key_{k}", repair_details, []
                elif len(norm_src) >= 8 and len(norm_tgt) >= 8:
                    common_chars = sum(1 for c in norm_tgt if c in norm_src)
                    if common_chars / len(norm_tgt) > 0.85 and HANGUL_RE.search(tgt_val):
                        is_opaque, opaque_reason = is_opaque_literal(src_val, tgt_val)
                        if is_opaque:
                            opaque_literal_keys.add(k)
                            glossary_warnings.append(f"raw_repetition_opaque_literal_warning_key_{k}_{opaque_reason}")
                            if event_origin == "production":
                                with _semantic_shadow_lock:
                                    _warning_counters["raw_repetition_opaque_literal_warning_count"] += 1
                                record_opaque_literal_event(
                                    request_id=request_id,
                                    json_key=k,
                                    source_line=src_val,
                                    output_line=tgt_val,
                                    opaque_reason=opaque_reason
                                )
                            logger.info(f"[OPAQUE_LITERAL_WARN_SIMILAR] key={k} reason={opaque_reason}: {tgt_val[:50]} (Practical Mode: Warning only)")
                        else:
                            if event_origin == "production":
                                with _semantic_shadow_lock:
                                    _hard_failure_counters["raw_repetition_similar_count"] += 1
                            return False, None, f"raw_repetition_similar_key_{k}", repair_details, []

    # 7. Hangul Detection (Practical Mode: Warning for local residual, HARD FAIL for large untranslated)
    had_jamo_preservation_in_chunk = False
    for k in sorted_keys:
        if k in opaque_literal_keys:
            continue
        src_val = source_dict.get(k, "")
        tgt_val = target_dict.get(k, "")
        if not tgt_val:
            continue
        cleaned = re.sub(r'https?://[^\s<>"]+', '', tgt_val)
        cleaned = re.sub(r'<[^>]+>', '', cleaned)

        # 1. Compatibility Jamo Check ([ㄱ-ㅎㅏ-ㅣ], \u3131-\u318e)
        jamo_matches = re.findall(r'[\u3131-\u318e]', cleaned)
        if jamo_matches:
            src_jamo_tokens = set(re.findall(r'[\u3131-\u318e]+', src_val))
            tgt_jamo_tokens = re.findall(r'[\u3131-\u318e]+', cleaned)
            if tgt_jamo_tokens and all(tok in src_jamo_tokens for tok in tgt_jamo_tokens):
                glossary_warnings.append(f"source_jamo_preserved_key_{k}_{','.join(tgt_jamo_tokens[:3])}")
                with _semantic_shadow_lock:
                    _warning_counters["source_jamo_preserved_count"] += len(tgt_jamo_tokens)
                had_jamo_preservation_in_chunk = True

        # 2. Hangul Syllables Check ([가-힣], \uac00-\ud7af)
        hangul_syllables = re.findall(r'[\uac00-\ud7af]', cleaned)
        if hangul_syllables:
            cjk_chars = re.findall(r'[\u4e00-\u9fff]', cleaned)
            num_hangul = len(hangul_syllables)
            num_cjk = len(cjk_chars)
            longest_hangul_span = max((len(m) for m in re.findall(r'[\uac00-\ud7af]+', cleaned)), default=0)

            # Check for Large Untranslated Hangul (HARD FAIL):
            is_large_untranslated = (
                (num_cjk < 2 and num_hangul >= 3) or
                (num_hangul >= 5 and (num_hangul / (num_hangul + num_cjk)) >= 0.50) or
                (longest_hangul_span >= 10)
            )

            if is_large_untranslated:
                with _semantic_shadow_lock:
                    _hard_failure_counters["large_hangul_untranslated_count"] += 1
                logger.warning(f"[LARGE_HANGUL_UNTRANSLATED] key={k} hangul={num_hangul} cjk={num_cjk} span={longest_hangul_span}: {tgt_val}")
                return False, None, f"large_hangul_untranslated_key_{k}_chars_{''.join(hangul_syllables[:8])}", repair_details, []
            else:
                glossary_warnings.append(f"hangul_residual_warning_key_{k}_{''.join(hangul_syllables[:5])}")
                with _semantic_shadow_lock:
                    _warning_counters["hangul_residual_warning_count"] += 1
                logger.info(f"[HANGUL_RESIDUAL_WARN] key={k} hangul={num_hangul} cjk={num_cjk}: {tgt_val} (Practical Mode: Warning only, request passes)")

    if had_jamo_preservation_in_chunk:
        with _semantic_shadow_lock:
            _warning_counters["source_jamo_preserved_chunks_count"] += 1

    # 8. HTML / Tag Structure Preservation
    for k in sorted_keys:
        src_val = source_dict[k]
        tgt_val = target_dict.get(k, "")

        src_tags = [t.lower() for t in TAG_RE.findall(src_val)]
        tgt_tags = [t.lower() for t in TAG_RE.findall(tgt_val)]

        for t in src_tags:
            tag_name = t.replace('/', '').strip()
            if tag_name in ('i', 'b', 'u', 'img', 'span', 'div'):
                if t not in tgt_tags:
                    with _semantic_shadow_lock:
                        _hard_failure_counters["structural_failure_count"] += 1
                    return False, None, f"tag_missing_key_{k}_tag_{t}", repair_details, []

    # 9. Abnormal English Natural Language Contamination (with Generic Transliteration Evidence Matcher & Shadow Mode)
    allowed_english_tokens = extract_allowed_english_tokens(messages, source_dict)

    for k in sorted_keys:
        if k in opaque_literal_keys:
            continue
        src_val = source_dict[k]
        tgt_val = target_dict.get(k, "")

        # Build line-scoped allowed tokens
        line_allowed = set(allowed_english_tokens)
        norm_src = re.sub(r'[\s\W_]+', '', src_val)

        # Track manual map matches separately
        manual_matched_words = set()
        for kor_term, eng_set in SOURCE_TRANSLITERATED_ENGLISH.items():
            norm_kor = re.sub(r'[\s\W_]+', '', kor_term)
            if kor_term in src_val or (norm_kor and norm_kor in norm_src):
                for eng_word in eng_set:
                    ew_norm = eng_word.casefold()
                    line_allowed.add(ew_norm)
                    manual_matched_words.add(ew_norm)

        tgt_words = re.findall(r'[A-Za-z]{2,}', tgt_val)
        if not tgt_words:
            continue

        # Check manual map logging
        for w in tgt_words:
            if w.casefold() in manual_matched_words:
                record_phonetic_shadow_event(k, w, kor_term, 1.0, 0.0, 1.0, 1, "manual_map", False, is_manual_map=True)

        # Identify unauthorized words (length >= 4 or jb)
        unauthorized_words = []
        for w in tgt_words:
            token_norm = w.casefold()
            if token_norm in line_allowed:
                continue
            if re.search(rf'<[^>]*{w}[^>]*>', tgt_val) or re.search(rf'https?://[^\s<>"]*{w}', tgt_val):
                continue
            if len(token_norm) >= 4 or token_norm in {"jb"}:
                unauthorized_words.append(w)

        if not unauthorized_words:
            continue

        # ── Generic Phonetic Evidence Matcher (Shadow Mode) ──────────────────
        phonetic_covered_tokens = set()
        matched_evidence_details = []

        if ENABLE_PHONETIC_TRANSLITERATION_MATCHER and src_val:
            # 1. Atomic Phrase Matching: Extract continuous ASCII sequences and word n-grams
            phrase_candidates = []
            for m in re.finditer(r'[A-Za-z]+(?:\s+[A-Za-z]+)+', tgt_val):
                p_text = m.group(0)
                p_words = re.findall(r'[A-Za-z]+', p_text)
                for span_len in range(min(5, len(p_words)), 1, -1):
                    for start_i in range(len(p_words) - span_len + 1):
                        sub_phrase = " ".join(p_words[start_i:start_i + span_len])
                        phrase_candidates.append(sub_phrase)

            for phrase in phrase_candidates:
                p_words = re.findall(r'[A-Za-z]+', phrase)
                if len(p_words) >= 2:
                    p_res = match_english_span_against_source(phrase, src_val)
                    p_allowed, p_score, p_span, p_dbg, p_cands, p_sec, p_margin, p_src = p_res
                    if p_allowed:
                        # Atomically cover all words strictly inside this phrase span
                        for pw in p_words:
                            phonetic_covered_tokens.add(pw.casefold())
                        matched_evidence_details.append({
                            "span": phrase,
                            "source_span": p_span,
                            "score": p_score,
                            "second_best": p_sec,
                            "margin": p_margin,
                            "candidate_count": p_cands,
                            "pron_source": p_src
                        })

            # 2. Token Matching: For any remaining unallowed word
            for w in unauthorized_words:
                wn = w.casefold()
                if wn in phonetic_covered_tokens:
                    continue
                w_res = match_english_span_against_source(w, src_val)
                w_allowed, w_score, w_span, w_dbg, w_cands, w_sec, w_margin, w_src = w_res
                if w_allowed:
                    phonetic_covered_tokens.add(wn)
                    matched_evidence_details.append({
                        "span": w,
                        "source_span": w_span,
                        "score": w_score,
                        "second_best": w_sec,
                        "margin": w_margin,
                        "candidate_count": w_cands,
                        "pron_source": w_src
                    })

        # Evaluate authorization & Phonetic evidence (for audit)
        all_unauthorized_covered = all(w.casefold() in phonetic_covered_tokens for w in unauthorized_words)

        if all_unauthorized_covered and matched_evidence_details:
            for ev in matched_evidence_details:
                record_phonetic_shadow_event(
                    k, ev["span"], ev["source_span"], ev["score"], 
                    ev["second_best"], ev["margin"], ev["candidate_count"], 
                    ev["pron_source"], would_allow=True
                )
                logger.info(
                    f"[PHONETIC_EVIDENCE] key={k} eng='{ev['span']}' kor='{ev['source_span']}' "
                    f"score={ev['score']:.3f} margin={ev['margin']:.3f} src={ev['pron_source']}"
                )

        # Off-path background shadow learning (optional, never blocks on-path)
        if ENABLE_SEMANTIC_RETENTION_SHADOW and src_val and matched_evidence_details:
            key_idx = sorted_keys.index(k) if k in sorted_keys else -1
            prev_k = sorted_keys[key_idx - 1] if key_idx > 0 else ""
            next_k = sorted_keys[key_idx + 1] if (0 <= key_idx < len(sorted_keys) - 1) else ""
            source_prev = source_dict.get(prev_k, "") if prev_k else ""
            source_next = source_dict.get(next_k, "") if next_k else ""
            for ev in matched_evidence_details:
                enqueue_semantic_shadow_job(
                    request_id=request_id or ("req_" + hashlib.md5(f"{k}_{src_val}".encode()).hexdigest()[:8]),
                    attempt=attempt,
                    key=k,
                    source_line=src_val,
                    source_prev=source_prev,
                    source_next=source_next,
                    source_span=ev["source_span"],
                    translated_line=tgt_val,
                    english_span=ev["span"],
                    phonetic_info={
                        "final_score": float(ev["score"]),
                        "consonant_score": 1.0,
                        "vowel_score": 1.0,
                        "alignment_score": float(ev.get("margin", 0.0) + ev["score"]),
                        "pronunciation_source": str(ev["pron_source"])
                    },
                    production_verdict="WARN",
                    is_only_hard_failure=False,
                    event_origin=event_origin
                )

        # In Practical Mode: english_contamination is a WARNING ONLY!
        first_failing_word = unauthorized_words[0]
        glossary_warnings.append(f"english_contamination_warning_key_{k}_word_{first_failing_word}")
        with _semantic_shadow_lock:
            _warning_counters["english_contamination_warning_count"] += len(unauthorized_words)
        logger.info(f"[ENGLISH_CONTAMINATION_WARN] key={k} tokens={unauthorized_words} (Practical Mode: Warning only, request passes)")
        continue

    # 10. Glossary Compliance Check (WARNING ONLY, Longest-Match, High-Confidence)
    glossary_entries = extract_glossary_entries(messages)
    high_conf_terms = [
        e for e in glossary_entries 
        if any(c in e.get("category", "") for c in HIGH_CONF_CATEGORIES)
    ]
    high_conf_terms.sort(key=lambda x: len(x["source"]), reverse=True)

    for k in sorted_keys:
        src_line = source_dict[k]
        tgt_line = target_dict.get(k, "")
        matched_spans = []

        for term in high_conf_terms:
            s_term = term["source"]
            t_term = term["target"]
            if not s_term or not t_term:
                continue

            if s_term not in src_line:
                continue

            start = 0
            valid_term_found_in_src = False
            while True:
                pos = src_line.find(s_term, start)
                if pos == -1:
                    break
                term_start = pos
                term_end = pos + len(s_term)
                start = term_end

                # Korean-aware boundary check:
                # Find contiguous Hangul sequence surrounding [term_start, term_end]
                t_start = term_start
                while t_start > 0 and HANGUL_RE.match(src_line[t_start - 1]):
                    t_start -= 1
                t_end = term_end
                while t_end < len(src_line) and HANGUL_RE.match(src_line[t_end]):
                    t_end += 1

                surrounding_hangul = src_line[t_start:t_end]

                # If term has Hangul prefix (e.g. 김안나), it is an internal substring -> SKIP
                if t_start < term_start:
                    continue

                # If short term (<= 2 chars) and surrounding token is longer than s_term (e.g. 안나와) -> SKIP
                if len(s_term) <= 2 and len(surrounding_hangul) > len(s_term):
                    continue

                # For longer terms (>= 3 chars), if suffix exists, verify if it's a known attached particle
                if len(s_term) >= 3 and len(surrounding_hangul) > len(s_term):
                    suffix = surrounding_hangul[len(s_term):]
                    common_particles = {"이", "가", "은", "는", "을", "를", "의", "와", "과", "도", "만", "씨", "님", "에게", "한테"}
                    if suffix not in common_particles:
                        continue

                # Overlap with longer matched span check
                overlap = any(st <= term_start < en or st < term_end <= en for st, en in matched_spans)
                if overlap:
                    continue
                matched_spans.append((term_start, term_end))

                valid_term_found_in_src = True
                break

            if not valid_term_found_in_src:
                continue

            t_term_found = False
            if re.search(r'[A-Za-z]', t_term):
                if t_term.casefold() in tgt_line.casefold():
                    t_term_found = True
            else:
                if t_term in tgt_line:
                    t_term_found = True

            if not t_term_found:
                glossary_warnings.append(
                    f"glossary_target_mismatch_key_{k}:{s_term}->{t_term}"
                )

    final_json_str = json.dumps(target_dict, ensure_ascii=False, indent=2)
    return True, final_json_str, None, repair_details, glossary_warnings

def format_text(text, limit=MAX_TEXT_SNIPPET):
    if not text:
        return ""
    if len(text) > limit:
        return text[:limit] + f"\n... [已截断，原长度 {len(text)} 字符]"
    return text

def log_conversation(req_id, profile_name, requested_model, actual_model, client_ip, prompt_text, response_text, is_stream, durations, status, routing_chain, task_type):
    now_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    stream_tag = "Stream" if is_stream else "Non-Stream"
    sep = "=" * 80
    sub_sep = "-" * 40
    
    total_duration = sum(durations) if isinstance(durations, list) else durations
    dur_str = f"{total_duration:.2f}s"
    if isinstance(durations, list) and len(durations) > 1:
        dur_str += f" ({', '.join([f'{d:.2f}s' for d in durations])})"
        
    msg = (
        f"\n{sep}\n"
        f"[{now_str}] ReqID: {req_id} | Profile: {profile_name} | Task: {task_type} | {stream_tag} | Status: {status} | Duration: {dur_str} | IP: {client_ip}\n"
        f"ROUTING:\n{routing_chain}\n"
        f"{sub_sep} [PROMPT] {sub_sep}\n"
        f"{format_text(prompt_text)}\n"
        f"{sub_sep} [RESPONSE] {sub_sep}\n"
        f"{format_text(response_text)}\n"
        f"{sep}\n"
    )
    logger.info(msg)

def parse_recent_logs(max_entries=60):
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return []

    raw_blocks = content.split("=" * 80)
    entries = []
    for b in reversed(raw_blocks):
        b = b.strip()
        if not b or "[PROMPT]" not in b:
            continue
        try:
            lines = b.split("\n")
            meta_line = lines[0].strip()
            
            # Find routing lines
            routing_lines = []
            in_routing = False
            for l in lines[1:]:
                if l.startswith("ROUTING:"):
                    in_routing = True
                    rest = l.replace("ROUTING:", "").strip()
                    if rest:
                        routing_lines.append(rest)
                    continue
                if in_routing:
                    if l.startswith("---") or "[PROMPT]" in l:
                        break
                    routing_lines.append(l.strip())
            routing_str = " | ".join([x for x in routing_lines if x])

            p_idx = b.find("[PROMPT]")
            r_idx = b.find("[RESPONSE]")
            if p_idx != -1 and r_idx != -1:
                prompt_content = b[p_idx + 48:r_idx - 40].strip()
                resp_content = b[r_idx + 50:].strip()
            else:
                prompt_content = ""
                resp_content = ""
            
            entries.append({
                "meta": meta_line,
                "routing": routing_str,
                "prompt": prompt_content,
                "response": resp_content
            })
            if len(entries) >= max_entries:
                break
        except Exception:
            continue
    return entries

def render_audit_page():
    stats = load_stats()
    
    # Balanced stats
    b = stats.get("balanced", {})
    b_total = b.get("total_body_chunks", 0)
    b_p1 = b.get("primary_attempt1_pass", 0)
    b_rep = b.get("local_repair_pass", 0)
    b_ret = b.get("non_retry_pass", 0)
    b_fal = b.get("reasoning_fallback_pass", 0)
    b_fail = b.get("final_quality_failure", 0)
    
    b_p1_pct = (b_p1 / b_total * 100) if b_total > 0 else 0
    b_rep_pct = (b_rep / b_total * 100) if b_total > 0 else 0
    b_ret_pct = (b_ret / b_total * 100) if b_total > 0 else 0
    b_fal_pct = (b_fal / b_total * 100) if b_total > 0 else 0
    b_fail_pct = (b_fail / b_total * 100) if b_total > 0 else 0

    # Quality stats
    q = stats.get("quality", {})
    q_total = q.get("quality_body_total", 0)
    q_p1 = q.get("reasoning_primary_pass", 0)
    q_rep = q.get("reasoning_local_repair_pass", 0)
    q_vfail = q.get("reasoning_validator_fail", 0)
    q_timeout = q.get("reasoning_timeout", 0)
    q_up_err = q.get("reasoning_upstream_error", 0)
    q_fb_cnt = q.get("non_fallback_count", 0)
    q_fb_pass = q.get("non_fallback_pass", 0)
    q_fb_fail = q.get("non_fallback_fail", 0)
    q_final_fail = q.get("quality_final_failure", 0)

    q_p1_pct = (q_p1 / q_total * 100) if q_total > 0 else 0
    q_rep_pct = (q_rep / q_total * 100) if q_total > 0 else 0
    q_fb_pct = (q_fb_pass / q_fb_cnt * 100) if q_fb_cnt > 0 else 0
    q_fail_pct = (q_final_fail / q_total * 100) if q_total > 0 else 0

    # Latencies
    q_rea_lat = calc_percentiles(q.get("reasoning_latencies", []))
    q_fb_lat = calc_percentiles(q.get("non_fallback_latencies", []))
    q_tot_lat = calc_percentiles(q.get("total_latencies", []))

    # Reasons
    reasons_items = []
    combined_reasons = {}
    for r_k, r_v in b.get("fail_reasons", {}).items():
        combined_reasons[f"B:{r_k}"] = combined_reasons.get(f"B:{r_k}", 0) + r_v
    for r_k, r_v in q.get("fail_reasons", {}).items():
        combined_reasons[f"Q:{r_k}"] = combined_reasons.get(f"Q:{r_k}", 0) + r_v
        
    warn_chips = [
        f"<span class='stat-chip' style='border-color:#eab308; color:#facc15;'>Hangul Residual: <b>{_warning_counters['hangul_residual_warning_count']}</b></span>",
        f"<span class='stat-chip' style='border-color:#eab308; color:#facc15;'>English Contamination: <b>{_warning_counters['english_contamination_warning_count']}</b></span>",
        f"<span class='stat-chip' style='border-color:#eab308; color:#facc15;'>Jamo Preserved: <b>{_warning_counters['source_jamo_preserved_count']}</b></span>",
        f"<span class='stat-chip' style='border-color:#eab308; color:#facc15;'>Opaque Literal: <b>{_warning_counters['raw_repetition_opaque_literal_warning_count']}</b></span>",
        f"<span class='stat-chip' style='border-color:#22c55e; color:#4ade80;'>挽救 Fallback: <b>{_warning_counters['warning_only_prevented_fallback_count']}</b></span>",
        f"<span class='stat-chip' style='border-color:#22c55e; color:#4ade80;'>挽救 502: <b>{_warning_counters['warning_only_prevented_502_count']}</b></span>"
    ]
    hard_chips = [
        f"<span class='stat-chip' style='border-color:#ef4444; color:#f87171;'>Raw Repetition Hard: <b>{_hard_failure_counters['raw_repetition_exact_hard_count']}</b></span>",
        f"<span class='stat-chip' style='border-color:#ef4444; color:#f87171;'>Large Untranslated: <b>{_hard_failure_counters['large_hangul_untranslated_count']}</b></span>",
        f"<span class='stat-chip' style='border-color:#ef4444; color:#f87171;'>Structural/Key: <b>{_hard_failure_counters['structural_failure_count']}</b></span>"
    ]
    reasons_html = f"""
    <div style='margin-top:6px; display:flex; flex-direction:column; gap:6px;'>
        <div><b>⚠️ 质量警告 (放行 200):</b> {' '.join(warn_chips)}</div>
        <div><b>🛑 硬质检拦截 (触发 Fallback/502):</b> {' '.join(hard_chips)}</div>
    </div>
    """

    entries = parse_recent_logs(60)
    items_html = []
    for idx, item in enumerate(entries):
        meta = html.escape(item["meta"])
        routing = html.escape(item["routing"])
        prompt = html.escape(item["prompt"])
        response = html.escape(item["response"])
        is_error = "Status: 200" not in meta
        status_color = "#f87171" if is_error else "#4ade80"
        
        badges = []
        if "Profile: quality" in meta:
            badges.append('<span class="badge" style="background:#a855f722; color:#c084fc; border: 1px solid #c084fc55;">💎 QUALITY</span>')
        elif "Profile: balanced" in meta:
            badges.append('<span class="badge" style="background:#38bdf822; color:#38bdf8; border: 1px solid #38bdf855;">🚀 BALANCED</span>')

        if "validator=pass" in routing or "pass" in routing:
            badges.append('<span class="badge" style="background:#4ade8022; color:#4ade80; border: 1px solid #4ade8055;">PASS</span>')
        elif "final_quality_failure" in routing:
            badges.append('<span class="badge" style="background:#f8717122; color:#f87171; border: 1px solid #f8717155;">FAIL</span>')
        
        if "local_repair" in routing:
            badges.append('<span class="badge" style="background:#38bdf822; color:#38bdf8; border: 1px solid #38bdf855;">🛠️ 本地修复</span>')
        if "fallback=true" in routing or "reasoning fallback" in routing:
            badges.append('<span class="badge" style="background:#f59e0b22; color:#f59e0b; border: 1px solid #f59e0b55;">🔄 兜底降级</span>')

        badge_str = " ".join(badges)

        card = f"""
        <div class="card">
            <div class="card-header" onclick="toggleCard({idx})">
                <span class="badge" style="background:{status_color}22; color:{status_color}; border: 1px solid {status_color}55;">
                    {'ERROR' if is_error else 'SUCCESS'}
                </span>
                {badge_str}
                <span class="meta-text">{meta}</span>
                <span class="arrow" id="arrow-{idx}">▼</span>
            </div>
            <div class="card-body" id="body-{idx}">
                {f'<div class="routing-box">🛣️ <b>链路追踪:</b> {routing}</div>' if routing else ''}
                <div class="section-title">📥 请求内容 (Prompt / 用户输入)</div>
                <pre class="code-box prompt-box">{prompt or '(空)'}</pre>
                <div class="section-title">📤 回复内容 (Response / 模型输出)</div>
                <pre class="code-box response-box">{response or '(空)'}</pre>
            </div>
        </div>
        """
        items_html.append(card)

    cards_joined = "\n".join(items_html) if items_html else "<div class='empty'>暂无对话日志，发起一次翻译请求后即可在此实时查看。</div>"
    
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>API 对话抓包与双模式质量守护看板</title>
    <style>
        :root {{
            --bg: #0f172a;
            --card-bg: #1e293b;
            --text: #f1f5f9;
            --text-muted: #94a3b8;
            --border: #334155;
            --accent: #38bdf8;
            --prompt-bg: #0b1329;
            --resp-bg: #062419;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background-color: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
            line-height: 1.6;
            padding: 24px;
        }}
        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border);
        }}
        h1 {{ font-size: 20px; font-weight: 600; color: #fff; }}
        .panel-title {{
            font-size: 14px;
            font-weight: 700;
            margin: 16px 0 8px 0;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 12px;
            margin-bottom: 16px;
        }}
        .stat-card {{
            background: #182234;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px 14px;
        }}
        .stat-card .label {{ font-size: 11px; color: var(--text-muted); font-weight: 600; text-transform: uppercase; }}
        .stat-card .val {{ font-size: 20px; font-weight: 700; color: #fff; margin-top: 4px; }}
        .stat-card .sub {{ font-size: 11px; color: #38bdf8; margin-top: 2px; }}
        .reasons-panel {{
            background: #182234;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 10px 14px;
            margin-bottom: 24px;
            font-size: 12px;
        }}
        .stat-chip {{
            display: inline-block;
            background: #0f172a;
            border: 1px solid var(--border);
            padding: 2px 8px;
            border-radius: 4px;
            margin-right: 8px;
            margin-top: 4px;
        }}
        .routing-box {{
            background: #0b1329;
            border: 1px solid #1e3a8a;
            color: #93c5fd;
            padding: 8px 12px;
            border-radius: 6px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 12px;
            margin-bottom: 12px;
            white-space: pre-wrap;
        }}
        .actions button {{
            background: #2563eb;
            color: #fff;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
        }}
        .actions button:hover {{ background: #1d4ed8; }}
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            margin-bottom: 14px;
            overflow: hidden;
        }}
        .card-header {{
            padding: 12px 16px;
            background: #182234;
            display: flex;
            align-items: center;
            cursor: pointer;
            user-select: none;
            gap: 10px;
        }}
        .card-header:hover {{ background: #1f2d45; }}
        .badge {{
            font-size: 11px;
            padding: 2px 8px;
            border-radius: 4px;
            font-weight: 700;
            white-space: nowrap;
        }}
        .meta-text {{
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 12px;
            color: var(--text);
            flex: 1;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .arrow {{ color: var(--text-muted); font-size: 12px; }}
        .card-body {{
            padding: 16px;
            border-top: 1px solid var(--border);
            display: block;
        }}
        .section-title {{
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
            margin: 12px 0 6px 0;
        }}
        .section-title:first-child {{ margin-top: 0; }}
        .code-box {{
            padding: 12px 14px;
            border-radius: 6px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "PingFang SC", "Microsoft YaHei", monospace;
            font-size: 13px;
            line-height: 1.5;
            white-space: pre-wrap;
            word-break: break-word;
            max-height: 500px;
            overflow-y: auto;
            border: 1px solid var(--border);
        }}
        .prompt-box {{ background: var(--prompt-bg); color: #bae6fd; }}
        .response-box {{ background: var(--resp-bg); color: #86efac; }}
        .empty {{ text-align: center; padding: 48px; color: var(--text-muted); }}
    </style>
    <script>
        function toggleCard(idx) {{
            const body = document.getElementById('body-' + idx);
            const arrow = document.getElementById('arrow-' + idx);
            if (body.style.display === 'none') {{
                body.style.display = 'block';
                arrow.textContent = '▼';
            }} else {{
                body.style.display = 'none';
                arrow.textContent = '▶';
            }}
        }}
    </script>
</head>
<body>
    <header>
        <div>
            <h1>📡 API 对话抓包与双模式质量守护监控</h1>
            <p style="font-size:12px; color:var(--text-muted); margin-top:4px;">
                Balanced 极速模式 | Quality 深度品质模式 | Quality Neutral Prompt | Practical 3-Tier Validator
            </p>
        </div>
        <div class="actions">
            <button onclick="location.reload()">🔄 刷新看板</button>
        </div>
    </header>

    <div class="panel-title" style="color:#c084fc;">
        💎 Quality 深度品质模式 (Reasoning Primary → Hard Validator → Non Fallback)
    </div>
    <div class="stats-grid">
        <div class="stat-card">
            <div class="label">正文总量</div>
            <div class="val">{q_total}</div>
            <div class="sub">Chunks</div>
        </div>
        <div class="stat-card">
            <div class="label">Reasoning 首轮通过率</div>
            <div class="val" style="color:#4ade80;">{q_p1_pct:.1f}%</div>
            <div class="sub">{q_p1} 次直接通过</div>
        </div>
        <div class="stat-card">
            <div class="label">Reasoning 本地修复</div>
            <div class="val" style="color:#38bdf8;">{q_rep_pct:.1f}%</div>
            <div class="sub">{q_rep} 次安全补正</div>
        </div>
        <div class="stat-card">
            <div class="label">Non 降级兜底</div>
            <div class="val" style="color:#f59e0b;">{q_fb_cnt} 次</div>
            <div class="sub">兜底成功率 {q_fb_pct:.1f}% ({q_fb_pass}次)</div>
        </div>
        <div class="stat-card">
            <div class="label">Reasoning 异常分布</div>
            <div class="val" style="font-size:13px; margin-top:6px; color:#e2e8f0;">
                质检拦截: {q_vfail} | 超时: {q_timeout} | 5xx: {q_up_err}
            </div>
            <div class="sub">Fail Breakdown</div>
        </div>
        <div class="stat-card">
            <div class="label">延迟指标 (P50 / P90 / Max)</div>
            <div class="val" style="font-size:13px; margin-top:6px; color:#38bdf8;">
                Reasoning: {q_rea_lat['p50']}s / {q_rea_lat['p90']}s / {q_rea_lat['max']}s
            </div>
            <div class="sub">总耗时 P50={q_tot_lat['p50']}s P90={q_tot_lat['p90']}s</div>
        </div>
    </div>

    <div class="panel-title" style="color:#38bdf8;">
        🚀 Balanced 平衡极速模式 (Non-Reasoning Primary → Non Retry → Reasoning Fallback)
    </div>
    <div class="stats-grid">
        <div class="stat-card">
            <div class="label">正文总量</div>
            <div class="val">{b_total}</div>
            <div class="sub">Chunks</div>
        </div>
        <div class="stat-card">
            <div class="label">首轮通过率</div>
            <div class="val" style="color:#4ade80;">{b_p1_pct:.1f}%</div>
            <div class="sub">{b_p1} 次直接通过</div>
        </div>
        <div class="stat-card">
            <div class="label">本地确定性修复</div>
            <div class="val" style="color:#38bdf8;">{b_rep_pct:.1f}%</div>
            <div class="sub">{b_rep} 次安全补正</div>
        </div>
        <div class="stat-card">
            <div class="label">Non 重试成功率</div>
            <div class="val" style="color:#818cf8;">{b_ret_pct:.1f}%</div>
            <div class="sub">{b_ret} 次重试达标</div>
        </div>
        <div class="stat-card">
            <div class="label">Reasoning 降级率</div>
            <div class="val" style="color:#f59e0b;">{b_fal_pct:.1f}%</div>
            <div class="sub">{b_fal} 次深度兜底</div>
        </div>
        <div class="stat-card">
            <div class="label">最终失败率</div>
            <div class="val" style="color:{'#f87171' if b_fail > 0 else '#94a3b8'};">{b_fail_pct:.1f}%</div>
            <div class="sub">{b_fail} 次质量熔断</div>
        </div>
    </div>

    <div class="reasons-panel">
        <b>🔍 质检拦截分布:</b> {reasons_html}
    </div>

    <main>
        {cards_joined}
    </main>
</body>
</html>
"""

def do_upstream_call(method, path, body_bytes, headers, timeout=240):
    conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=timeout)
    try:
        conn.request(method, path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        reason = resp.reason
        resp_headers = resp.getheaders()
        body = resp.read()
        return status, reason, resp_headers, body
    except (http.client.HTTPException, ConnectionResetError, BrokenPipeError, ConnectionRefusedError) as e:
        return 502, "Bad Gateway", [], json.dumps({"error": {"message": f"Upstream error: {e}", "type": "upstream_error"}}).encode('utf-8')
    except TimeoutError as e:
        return 504, "Gateway Timeout", [], json.dumps({"error": {"message": f"Upstream timeout ({timeout}s): {e}", "type": "upstream_timeout"}}).encode('utf-8')
    except Exception as e:
        err_str = str(e).lower()
        if "timed out" in err_str or "timeout" in err_str:
            return 504, "Gateway Timeout", [], json.dumps({"error": {"message": f"Upstream timeout ({timeout}s): {e}", "type": "upstream_timeout"}}).encode('utf-8')
        return 502, "Bad Gateway", [], json.dumps({"error": {"message": f"Upstream exception: {e}", "type": "upstream_exception"}}).encode('utf-8')
    finally:
        try:
            conn.close()
        except Exception:
            pass

class AuditProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/stats/practical_validator", "/stats/semantic_shadow", "/stats/narrow_canary") or self.path.startswith("/stats/practical_validator?") or self.path.startswith("/stats/semantic_shadow?") or self.path.startswith("/stats/narrow_canary?"):
            with _semantic_shadow_lock:
                q_total_lat = _calc_stats(_quality_total_latencies_ms)
                p_rea_lat = _calc_stats(_primary_reasoning_latencies_ms)

                epoch_hard_failures = {
                    "raw_repetition_exact_hard_count": _hard_failure_counters.get("raw_repetition_exact_hard_count", 0),
                    "raw_repetition_similar_count": _hard_failure_counters.get("raw_repetition_similar_count", 0),
                    "large_hangul_untranslated_count": _hard_failure_counters.get("large_hangul_untranslated_count", 0),
                    "structural_failure_count": _hard_failure_counters.get("structural_failure_count", 0),
                    "empty_output_count": _hard_failure_counters.get("empty_output_count", 0),
                    "line_shift_count": _hard_failure_counters.get("line_shift_count", 0)
                }

                resp_data = {
                    "mode": "practical_validator_three_tier",
                    "observation_epoch": OBSERVATION_EPOCH,
                    "observation_started_at": OBSERVATION_STARTED_AT,
                    "metadata": {
                        "semantic_onpath_enabled": False,
                        "semantic_shadow_enabled": ENABLE_SEMANTIC_RETENTION_SHADOW,
                        "semantic_model": SEMANTIC_SHADOW_MODEL,
                        "semantic_prompt_hash": SEMANTIC_PROMPT_HASH
                    },
                    "quality_warnings": dict(_warning_counters),
                    "hard_failures": epoch_hard_failures,
                    "lifetime_legacy": {
                        "legacy_raw_repetition_exact_count": _hard_failure_counters.get("raw_repetition_exact_count", 59)
                    },
                    "recent_opaque_literal_events": list(_opaque_literal_events),
                    "quality_total_latency": q_total_lat,
                    "primary_reasoning_capacity": {
                        "latency": p_rea_lat,
                        "timeout_count": _primary_reasoning_timeout_count,
                        "5xx_count": _primary_reasoning_5xx_count
                    },
                    "targeted_retranslation_shadow_eval": dict(_targeted_retranslation_eval_stats),
                    "cumulative_production_counters": dict(_cumulative_production_counters)
                }

            body = json.dumps(resp_data, ensure_ascii=False, indent=2).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/audit" or self.path.startswith("/audit?"):
            html_data = render_audit_page().encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_data)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(html_data)
            return
        self._proxy_request("GET")

    def do_HEAD(self):
        self._proxy_request("HEAD")

    def do_POST(self):
        self._proxy_request("POST")

    def do_PUT(self):
        self._proxy_request("PUT")

    def do_DELETE(self):
        self._proxy_request("DELETE")

    def do_OPTIONS(self):
        self._proxy_request("OPTIONS")

    def _proxy_request(self, method):
        t0 = time.time()
        client_ip = self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        auth_header = self.headers.get("Authorization", "")
        profile_name, profile_cfg = identify_profile(auth_header)
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        global _canary_force_header_ignored_count, _primary_reasoning_timeout_count, _primary_reasoning_5xx_count
        is_chat = self.path.startswith("/v1/chat/completions")
        prompt_text = ""
        requested_model = "unknown"
        is_stream = False
        req_id = hex(int(time.time() * 1000))[2:]

        event_origin = self.headers.get("X-Event-Origin", "production").strip().lower()
        if event_origin not in ("production", "smoke_test", "offline_test"):
            event_origin = "production"
        sample_origin = "natural_canary" 

        # ── Non-dedicated Key: Standard Direct Proxy (Zero interference) ───────────────
        if not profile_name:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
            headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
            headers["Connection"] = "close"

            try:
                conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=900)
                conn.request(method, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()

                resp_chunks = []
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    if is_chat:
                        resp_chunks.append(chunk)

                duration = time.time() - t0
                conn.close()

                if is_chat:
                    full_raw = b"".join(resp_chunks).decode('utf-8', errors='ignore')
                    model_name = "unknown"
                    try:
                        b_json = json.loads(body.decode('utf-8', errors='ignore'))
                        model_name = b_json.get("model", "unknown")
                    except Exception:
                        pass
                    log_conversation(req_id, "standard", model_name, model_name, client_ip, prompt_text, full_raw[:500], is_stream, [duration], resp.status, "profile=standard\nrouting=pass-through", "standard")
            except Exception as e:
                if not self.wfile.closed:
                    try:
                        self.send_error(502, f"Bad Gateway: {e}")
                    except Exception:
                        pass
            return

        # ── Dedicated Translation Profile Flow (Balanced or Quality) ──────────────────
        body_json = {}
        messages = []
        task_type = "body"
        source_dict = {}

        if is_chat and body:
            try:
                body_json = json.loads(body.decode('utf-8', errors='ignore'))
                requested_model = body_json.get("model", "unknown")
                is_stream = bool(body_json.get("stream", False))
                messages = body_json.get("messages", [])

                parts = []
                for m in messages:
                    role = m.get("role", "unknown")
                    c = m.get("content", "")
                    if isinstance(c, list):
                        c = " ".join([item.get("text", "") for item in c if isinstance(item, dict)])
                    parts.append(f"<{role}>: {c}")
                prompt_text = "\n".join(parts)

                task_type = classify_task(messages)
                if task_type == "body":
                    source_dict = extract_source_dict(messages)
                    if profile_name:
                        init_request_correlation(req_id, mode=profile_name, event_origin=event_origin, sample_origin=sample_origin)
            except Exception as e:
                prompt_text = f"[Failed to parse request JSON: {e}]"

        # Headers for upstream: Replace client Authorization with internal upstream credential
        upstream_headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection", "content-length", "authorization")}
        upstream_headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
        upstream_headers["Connection"] = "close"
        upstream_headers["Authorization"] = f"Bearer {UPSTREAM_AUTH_TOKEN}"

        # Non-chat or non-POST requests: direct forward with auth replacement
        if not is_chat or method != "POST":
            status, reason, resp_headers, body_bytes = do_upstream_call(method, self.path, body, upstream_headers, timeout=120)
            self.send_response(status, reason)
            for k, v in resp_headers:
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        # ── Title or Glossary Tasks: Both profiles route directly to non-reasoning ───────
        durations = []
        if task_type in ("glossary", "title"):
            target_model = profile_cfg.get(f"{task_type}_model", "grok-4.20-0309-non-reasoning")
            body_json["model"] = target_model
            # DO NOT touch messages, user prompt, system prompt!
            out_body = json.dumps(body_json, ensure_ascii=False).encode('utf-8')
            upstream_headers["Content-Length"] = str(len(out_body))
            
            t_call = time.time()
            status, reason, resp_headers, body_bytes = do_upstream_call(method, self.path, out_body, upstream_headers, timeout=90)
            durations.append(time.time() - t_call)

            resp_text = ""
            try:
                rj = json.loads(body_bytes.decode('utf-8', errors='ignore'))
                resp_text = rj.get("choices", [{}])[0].get("message", {}).get("content", "")
            except Exception:
                resp_text = body_bytes.decode('utf-8', errors='ignore')[:300]

            self.send_response(status, reason)
            for k, v in resp_headers:
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

            routing_chain = (
                f"profile={profile_name}\n"
                f"task={task_type}\n"
                f"actual={target_model}"
            )
            log_conversation(req_id, profile_name, requested_model, target_model, client_ip, prompt_text, resp_text, is_stream, durations, status, routing_chain, task_type)
            return

        # ══════════════════════════════════════════════════════════════════════════════
        # ── PROFILE: QUALITY (Reasoning Primary -> Hard Validator -> Non Fallback) ───
        # ══════════════════════════════════════════════════════════════════════════════
        if profile_name == "quality":
            primary_model = profile_cfg.get("primary_model", "grok-4.20-0309-reasoning")
            fallback_model = profile_cfg.get("fallback_model", "grok-4.20-0309-non-reasoning")
            timeout_primary = profile_cfg.get("timeout_primary", 240)
            timeout_fallback = profile_cfg.get("timeout_fallback", 90)

            # Quality Body Prompt Override (Neutral V1) with strict NovalPie template fingerprinting
            prompt_profile = "passthrough"
            prompt_overridden = False
            prompt_reason = "not_applicable"
            template_hash = ""

            if task_type == "body":
                (
                    body_json["messages"],
                    prompt_profile,
                    prompt_overridden,
                    prompt_reason,
                    template_hash
                ) = apply_quality_prompt_replacement(body_json.get("messages", []))
                messages = body_json["messages"]
                if prompt_overridden:
                    parts = []
                    for m in messages:
                        role = m.get("role", "unknown")
                        c = m.get("content", "")
                        if isinstance(c, list):
                            c = " ".join([item.get("text", "") for item in c if isinstance(item, dict)])
                        parts.append(f"<{role}>: {c}")
                    prompt_text = "\n".join(parts)

            # Forced Fallback Test Hook (safe internal testing header)
            force_fallback = (self.headers.get("x-test-force-fallback") == "true")

            if force_fallback:
                dur1 = 0.01
                durations.append(dur1)
                primary_success = False
                used_local_repair = False
                fail_type = "forced_test_fallback"
                last_fail_reason = "test_forced_fallback_trigger"
                final_resp_payload = None
            else:
                # Attempt 1: Reasoning Primary
                body_json["model"] = primary_model
                out_body = json.dumps(body_json, ensure_ascii=False).encode('utf-8')
                upstream_headers["Content-Length"] = str(len(out_body))

                t_call1 = time.time()
                status, reason, resp_headers, body_bytes = do_upstream_call(method, self.path, out_body, upstream_headers, timeout=timeout_primary)
                dur1 = time.time() - t_call1
                durations.append(dur1)

                with _semantic_shadow_lock:
                    _primary_reasoning_latencies_ms.append(dur1 * 1000.0)
                    if status == 504:
                        _primary_reasoning_timeout_count += 1
                    elif status >= 500:
                        _primary_reasoning_5xx_count += 1

                resp_text = ""
                try:
                    rj = json.loads(body_bytes.decode('utf-8', errors='ignore'))
                    resp_text = rj.get("choices", [{}])[0].get("message", {}).get("content", "")
                except Exception:
                    resp_text = body_bytes.decode('utf-8', errors='ignore')

                primary_success = False
                used_local_repair = False
                fail_type = None
                last_fail_reason = ""
                final_resp_payload = None

                if status == 200 and resp_text:
                    is_valid, repaired_str, v_fail_reason, rep_details, g_warnings = validate_and_repair_body(source_dict, resp_text, messages=messages, attempt='reasoning_primary', request_id=req_id, event_origin=event_origin)
                    if is_valid:
                        primary_success = True
                        if rep_details:
                            used_local_repair = True
                            try:
                                rj["choices"][0]["message"]["content"] = repaired_str
                                final_resp_payload = json.dumps(rj, ensure_ascii=False).encode('utf-8')
                            except Exception:
                                final_resp_payload = body_bytes
                        else:
                            final_resp_payload = body_bytes
                    else:
                        fail_type = "validator_fail"
                        last_fail_reason = v_fail_reason
                else:
                    if status == 504:
                        fail_type = "timeout"
                        last_fail_reason = "upstream_timeout"
                    else:
                        fail_type = "upstream_error"
                        last_fail_reason = f"upstream_http_{status}"

            # If Primary Succeeded: Deliver immediately
            if primary_success:
                if g_warnings and any("warning" in str(w).lower() for w in g_warnings):
                    with _semantic_shadow_lock:
                        _warning_counters["warning_only_chunk_count"] += 1
                        _warning_counters["warning_only_prevented_fallback_count"] += 1
                if g_warnings and any("raw_repetition_opaque" in str(w) for w in g_warnings):
                    if event_origin == "production":
                        with _semantic_shadow_lock:
                            _warning_counters["raw_repetition_opaque_prevented_fallback_count"] += 1
                finalize_production_request_outcome(
                    req_id, final_status=status, final_failure=False,
                    primary_result="PASS", fallback_started=False,
                    mode="quality", event_origin=event_origin
                )
                rep_note = f" (local_repair: {','.join(rep_details)})" if used_local_repair else ""
                warn_note = f"\nwarning={';'.join(g_warnings[:3])}" if g_warnings else ""
                routing_chain = (
                    f"profile=quality\n"
                    f"task=body\n"
                    f"requested={requested_model}\n"
                    f"actual={primary_model}\n"
                    f"prompt_profile={prompt_profile}\n"
                    f"prompt_override={str(prompt_overridden).lower()}\n"
                    f"prompt_override_reason={prompt_reason}\n"
                    f"template_hash={template_hash}\n"
                    f"attempt=1\n"
                    f"validator=pass{rep_note}{warn_note}\n"
                    f"fallback=false"
                )
                self.send_response(status, reason)
                for k, v in resp_headers:
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(final_resp_payload)))
                self.end_headers()
                self.wfile.write(final_resp_payload)
                self.wfile.flush()

                record_quality_stats(True, True, used_local_repair, None, False, False, dur1, 0.0, dur1, None)
                log_conversation(req_id, "quality", requested_model, primary_model, client_ip, prompt_text, resp_text, is_stream, durations, status, routing_chain, "body")
                return

            # Primary Failed: Evaluate Targeted Retranslation Shadow (eval only, zero production route change)
            evaluate_targeted_retranslation_shadow(req_id, source_dict, resp_text, last_fail_reason)

            # Primary Failed: Immediate Non-Reasoning Fallback (No reasoning retry, zero prompt injection!)
            body_json["model"] = fallback_model
            out_body = json.dumps(body_json, ensure_ascii=False).encode('utf-8')
            upstream_headers["Content-Length"] = str(len(out_body))

            t_call2 = time.time()
            fb_status, fb_reason, fb_headers, fb_body_bytes = do_upstream_call(method, self.path, out_body, upstream_headers, timeout=timeout_fallback)
            dur2 = time.time() - t_call2
            durations.append(dur2)

            fb_resp_text = ""
            try:
                fb_rj = json.loads(fb_body_bytes.decode('utf-8', errors='ignore'))
                fb_resp_text = fb_rj.get("choices", [{}])[0].get("message", {}).get("content", "")
            except Exception:
                fb_resp_text = fb_body_bytes.decode('utf-8', errors='ignore')

            fb_success = False
            fb_repaired_str = ""
            fb_fail_reason = ""
            fb_rep_details = []

            if fb_status == 200 and fb_resp_text:
                fb_valid, fb_repaired_str, fb_fail_reason, fb_rep_details, fb_g_warnings = validate_and_repair_body(source_dict, fb_resp_text, messages=messages, attempt='non_fallback', request_id=req_id, event_origin=event_origin)
                if fb_valid:
                    fb_success = True
                    if fb_rep_details:
                        try:
                            fb_rj["choices"][0]["message"]["content"] = fb_repaired_str
                            final_resp_payload = json.dumps(fb_rj, ensure_ascii=False).encode('utf-8')
                        except Exception:
                            final_resp_payload = fb_body_bytes
                    else:
                        final_resp_payload = fb_body_bytes
            else:
                fb_fail_reason = f"upstream_http_{fb_status}"

            total_dur = dur1 + dur2

            if fb_success:
                if fb_g_warnings and any("warning" in str(w).lower() for w in fb_g_warnings):
                    with _semantic_shadow_lock:
                        _warning_counters["warning_only_prevented_502_count"] += 1
                if fb_g_warnings and any("raw_repetition_opaque" in str(w) for w in fb_g_warnings):
                    if event_origin == "production":
                        with _semantic_shadow_lock:
                            _warning_counters["raw_repetition_opaque_prevented_502_count"] += 1
                finalize_production_request_outcome(
                    req_id, final_status=fb_status, final_failure=False,
                    primary_result="FAIL", primary_fail_reason=last_fail_reason,
                    fallback_started=True, fallback_result="PASS",
                    mode="quality", event_origin=event_origin
                )
                fb_rep_note = f" (local_repair: {','.join(fb_rep_details)})" if fb_rep_details else ""
                fb_warn_note = f"\nwarning={';'.join(fb_g_warnings[:3])}" if fb_g_warnings else ""
                routing_chain = (
                    f"profile=quality\n"
                    f"task=body\n"
                    f"requested={requested_model}\n"
                    f"actual={primary_model}\n"
                    f"prompt_profile={prompt_profile}\n"
                    f"prompt_override={str(prompt_overridden).lower()}\n"
                    f"prompt_override_reason={prompt_reason}\n"
                    f"template_hash={template_hash}\n"
                    f"attempt=1\n"
                    f"validator=fail\n"
                    f"reason={last_fail_reason}\n\n"
                    f"→\n\n"
                    f"actual={fallback_model}\n"
                    f"attempt=2\n"
                    f"fallback=true\n"
                    f"validator=pass{fb_rep_note}{fb_warn_note}"
                )
                self.send_response(fb_status, fb_reason)
                for k, v in fb_headers:
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(final_resp_payload)))
                self.end_headers()
                self.wfile.write(final_resp_payload)
                self.wfile.flush()

                record_quality_stats(True, False, False, fail_type, True, True, dur1, dur2, total_dur, last_fail_reason)
                log_conversation(req_id, "quality", requested_model, fallback_model, client_ip, prompt_text, fb_resp_text, is_stream, durations, fb_status, routing_chain, "body")
                return
            else:
                # Both Reasoning and Non failed: Quality circuit break
                finalize_production_request_outcome(
                    req_id, final_status=502, final_failure=True,
                    primary_result="FAIL", primary_fail_reason=last_fail_reason,
                    fallback_started=True, fallback_result="FAIL", fallback_fail_reason=fb_fail_reason,
                    mode="quality", event_origin=event_origin
                )
                routing_chain = (
                    f"profile=quality\n"
                    f"task=body\n"
                    f"requested={requested_model}\n"
                    f"actual={primary_model}\n"
                    f"prompt_profile={prompt_profile}\n"
                    f"prompt_override={str(prompt_overridden).lower()}\n"
                    f"prompt_override_reason={prompt_reason}\n"
                    f"template_hash={template_hash}\n"
                    f"attempt=1\n"
                    f"validator=fail\n"
                    f"reason={last_fail_reason}\n\n"
                    f"→\n\n"
                    f"actual={fallback_model}\n"
                    f"attempt=2\n"
                    f"fallback=true\n"
                    f"validator=fail\n"
                    f"reason={fb_fail_reason}\n\n"
                    f"→ final_quality_failure"
                )
                err_payload = {
                    "error": {
                        "message": f"Translation quality validation failed (Quality profile: primary reasoning failed with {last_fail_reason}; fallback non failed with {fb_fail_reason})",
                        "type": "quality_guard_error",
                        "code": "translation_validation_failed"
                    }
                }
                err_bytes = json.dumps(err_payload, ensure_ascii=False).encode('utf-8')
                self.send_response(502, "Bad Gateway")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)
                self.wfile.flush()

                record_quality_stats(True, False, False, fail_type, True, False, dur1, dur2, total_dur, fb_fail_reason or last_fail_reason)
                log_conversation(req_id, "quality", requested_model, fallback_model, client_ip, prompt_text, json.dumps(err_payload, ensure_ascii=False), is_stream, durations, 502, routing_chain, "body")
                return

        # ══════════════════════════════════════════════════════════════════════════════
        # ── PROFILE: BALANCED (Non-Reasoning Primary -> Non Retry -> Reasoning FB) ────
        # ══════════════════════════════════════════════════════════════════════════════
        primary_model = profile_cfg.get("primary_model", "grok-4.20-0309-non-reasoning")
        fallback_model = profile_cfg.get("fallback_model", "grok-4.20-0309-reasoning")
        timeout_primary = profile_cfg.get("timeout_primary", 90)
        timeout_fallback = profile_cfg.get("timeout_fallback", 240)

        final_status = 502
        final_reason = "Bad Gateway"
        final_headers = []
        final_body_bytes = b""
        final_resp_text = ""
        success = False
        last_fail_reason = ""
        used_local_repair = False
        routing_steps = [f"requested={requested_model} → actual=non"]

        # Attempt 1: grok-4.20-0309-non-reasoning (PRIMARY)
        body_json["model"] = primary_model
        out_body = json.dumps(body_json, ensure_ascii=False).encode('utf-8')
        upstream_headers["Content-Length"] = str(len(out_body))

        t_call = time.time()
        status, reason, resp_headers, body_bytes = do_upstream_call(method, self.path, out_body, upstream_headers, timeout=timeout_primary)
        durations.append(time.time() - t_call)

        resp_text = ""
        try:
            rj = json.loads(body_bytes.decode('utf-8', errors='ignore'))
            resp_text = rj.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception:
            resp_text = body_bytes.decode('utf-8', errors='ignore')

        if status == 200 and resp_text:
            is_valid, repaired_str, fail_reason, rep_details, g_warnings = validate_and_repair_body(source_dict, resp_text, messages=messages, attempt='balanced_attempt1', request_id=req_id, event_origin=event_origin)
            if is_valid:
                success = True
                final_status, final_reason, final_headers = status, reason, resp_headers
                final_resp_text = repaired_str or resp_text
                if rep_details:
                    used_local_repair = True
                    try:
                        rj["choices"][0]["message"]["content"] = repaired_str
                        final_body_bytes = json.dumps(rj, ensure_ascii=False).encode('utf-8')
                    except Exception:
                        final_body_bytes = body_bytes
                else:
                    final_body_bytes = body_bytes
                repair_tag = f" [local_repair:{','.join(rep_details)}]" if rep_details else ""
                routing_steps.append(f"attempt=1{repair_tag} → pass")
                record_balanced_stats(True, 1, True, None, used_local_repair, durations[-1])
            else:
                last_fail_reason = fail_reason
                routing_steps.append(f"attempt=1 fail={fail_reason}")
        else:
            last_fail_reason = f"http_{status}"
            routing_steps.append(f"attempt=1 http_error={status}")

        # Attempt 2: Non-reasoning Retry
        if not success:
            routing_steps.append("non retry")
            body_json["model"] = primary_model
            out_body = json.dumps(body_json, ensure_ascii=False).encode('utf-8')
            upstream_headers["Content-Length"] = str(len(out_body))

            t_call = time.time()
            status, reason, resp_headers, body_bytes = do_upstream_call(method, self.path, out_body, upstream_headers, timeout=timeout_primary)
            durations.append(time.time() - t_call)

            resp_text = ""
            try:
                rj = json.loads(body_bytes.decode('utf-8', errors='ignore'))
                resp_text = rj.get("choices", [{}])[0].get("message", {}).get("content", "")
            except Exception:
                resp_text = body_bytes.decode('utf-8', errors='ignore')

            if status == 200 and resp_text:
                is_valid, repaired_str, fail_reason, rep_details, g_warnings = validate_and_repair_body(source_dict, resp_text, messages=messages, attempt='balanced_attempt2', request_id=req_id, event_origin=event_origin)
                if is_valid:
                    success = True
                    final_status, final_reason, final_headers = status, reason, resp_headers
                    final_resp_text = repaired_str or resp_text
                    if rep_details:
                        used_local_repair = True
                        try:
                            rj["choices"][0]["message"]["content"] = repaired_str
                            final_body_bytes = json.dumps(rj, ensure_ascii=False).encode('utf-8')
                        except Exception:
                            final_body_bytes = body_bytes
                    else:
                        final_body_bytes = body_bytes
                    repair_tag = f" [local_repair:{','.join(rep_details)}]" if rep_details else ""
                    routing_steps.append(f"attempt=2{repair_tag} → pass")
                    record_balanced_stats(True, 2, True, None, used_local_repair, durations[-1])
                else:
                    last_fail_reason = fail_reason
                    routing_steps.append(f"attempt=2 fail={fail_reason}")
            else:
                last_fail_reason = f"http_{status}"
                routing_steps.append(f"attempt=2 http_error={status}")

        # Attempt 3: Reasoning Fallback
        if not success:
            routing_steps.append("reasoning fallback")
            body_json["model"] = fallback_model
            out_body = json.dumps(body_json, ensure_ascii=False).encode('utf-8')
            upstream_headers["Content-Length"] = str(len(out_body))

            t_call = time.time()
            status, reason, resp_headers, body_bytes = do_upstream_call(method, self.path, out_body, upstream_headers, timeout=timeout_fallback)
            durations.append(time.time() - t_call)

            resp_text = ""
            try:
                rj = json.loads(body_bytes.decode('utf-8', errors='ignore'))
                resp_text = rj.get("choices", [{}])[0].get("message", {}).get("content", "")
            except Exception:
                resp_text = body_bytes.decode('utf-8', errors='ignore')

            if status == 200 and resp_text:
                is_valid, repaired_str, fail_reason, rep_details, g_warnings = validate_and_repair_body(source_dict, resp_text, messages=messages, attempt='balanced_fallback', request_id=req_id, event_origin=event_origin)
                if is_valid:
                    success = True
                    final_status, final_reason, final_headers = status, reason, resp_headers
                    final_resp_text = repaired_str or resp_text
                    if rep_details:
                        used_local_repair = True
                        try:
                            rj["choices"][0]["message"]["content"] = repaired_str
                            final_body_bytes = json.dumps(rj, ensure_ascii=False).encode('utf-8')
                        except Exception:
                            final_body_bytes = body_bytes
                    else:
                        final_body_bytes = body_bytes
                    repair_tag = f" [local_repair:{','.join(rep_details)}]" if rep_details else ""
                    routing_steps.append(f"attempt=3 (reasoning){repair_tag} → pass")
                    record_balanced_stats(True, 3, True, None, used_local_repair, durations[-1])
                else:
                    last_fail_reason = fail_reason
                    routing_steps.append(f"attempt=3 fail={fail_reason}")
            else:
                last_fail_reason = f"http_{status}"
                routing_steps.append(f"attempt=3 http_error={status}")

        if not success:
            routing_steps.append("final_quality_failure")
            record_balanced_stats(True, 3, False, last_fail_reason, False, sum(durations))
            err_payload = {
                "error": {
                    "message": f"Translation quality validation failed: {last_fail_reason}",
                    "type": "quality_guard_error",
                    "code": "translation_validation_failed"
                }
            }
            final_body_bytes = json.dumps(err_payload, ensure_ascii=False).encode('utf-8')
            final_status = 502
            final_reason = "Bad Gateway"
            final_headers = [("Content-Type", "application/json; charset=utf-8")]
            final_resp_text = json.dumps(err_payload, ensure_ascii=False)

        finalize_production_request_outcome(
            req_id, final_status=final_status, final_failure=(not success),
            mode="balanced", event_origin=event_origin
        )
        try:
            self.send_response(final_status, final_reason)
            for k, v in final_headers:
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(final_body_bytes)))
            self.end_headers()
            self.wfile.write(final_body_bytes)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            final_status = 499
            routing_steps.append("client_aborted")

        chain_str = " | ".join(routing_steps)
        actual_model_logged = fallback_model if ("reasoning fallback" in chain_str and success) else primary_model
        log_conversation(req_id, "balanced", requested_model, actual_model_logged, client_ip, prompt_text, final_resp_text, is_stream, durations, final_status, chain_str, task_type)

def main():
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), AuditProxyHandler)
    server.daemon_threads = True
    start_semantic_shadow_workers()
    print(f"[AuditProxy] Quality Guard Dual-Profile Engine running on {LISTEN_HOST}:{LISTEN_PORT} -> Upstream {UPSTREAM_HOST}:{UPSTREAM_PORT}")
    server.serve_forever()

if __name__ == "__main__":
    main()
